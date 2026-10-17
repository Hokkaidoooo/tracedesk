"""Thread-lifecycle protocol fixtures; these do not measure ONNX model quality."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from types import SimpleNamespace

import pytest

from tracedesk.knowledge.config import KnowledgeConfig
from tracedesk.knowledge.embeddings import OnnxBGEEmbedder

THREAD_WAIT_SECONDS = 5


async def wait_for_thread_event(event: threading.Event) -> None:
    async def observe():
        while not event.is_set():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(observe(), THREAD_WAIT_SECONDS)


@pytest.mark.parametrize("interruption", ["cancel", "timeout"])
def test_interruption_does_not_return_while_encoding_thread_is_alive(interruption) -> None:
    async def scenario():
        started, release, finished = (threading.Event() for _ in range(3))

        def blocked_encode(*_arguments):
            started.set()
            try:
                assert release.wait(THREAD_WAIT_SECONDS), "The test did not release its worker."
                return ((1.0, 0.0),)
            finally:
                finished.set()

        embedder = OnnxBGEEmbedder.__new__(OnnxBGEEmbedder)
        embedder._run_options = lambda: SimpleNamespace(terminate=False)
        embedder._encode = blocked_encode
        encoding = asyncio.create_task(embedder.embed(("thread lifecycle fixture",)))
        await wait_for_thread_event(started)
        if interruption == "cancel":
            public_task = encoding
            encoding.cancel()
        else:
            public_task = asyncio.create_task(asyncio.wait_for(encoding, timeout=0))
        try:
            done, _ = await asyncio.wait({public_task}, timeout=0.05)
            returned_before_worker_exit = bool(done) and not finished.is_set()
        finally:
            release.set()
            await wait_for_thread_event(finished)
            outcome = (await asyncio.gather(public_task, return_exceptions=True))[0]
        assert not returned_before_worker_exit, (
            f"{interruption} returned to the caller while the encoding thread was still alive."
        )
        expected = asyncio.CancelledError if interruption == "cancel" else asyncio.TimeoutError
        assert isinstance(outcome, expected)

    asyncio.run(scenario())


class RunOptionsFixture:
    def __init__(self):
        self.terminated = threading.Event()

    @property
    def terminate(self):
        return self.terminated.is_set()

    @terminate.setter
    def terminate(self, value):
        assert value is True
        self.terminated.set()


class ThreadGate:
    def __init__(self, *, blocked=False):
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        if not blocked:
            self.release.set()

    @contextmanager
    def active(self):
        self.started.set()
        try:
            assert self.release.wait(THREAD_WAIT_SECONDS), "The test did not release its worker."
            yield
        finally:
            self.finished.set()


class TokenizerFixture:
    def __init__(self, *, blocked=False, error=None):
        self.gate = ThreadGate(blocked=blocked)
        self.error = error
        self.calls = []

    def encode_batch(self, texts):
        self.calls.append(tuple(texts))
        with self.gate.active():
            if self.error is not None:
                raise self.error
            return [
                SimpleNamespace(
                    ids=[101, 7], attention_mask=[1, 1], type_ids=[0, 0], overflowing=[]
                )
                for _ in texts
            ]


class HiddenStateFixture:
    def __init__(self, count):
        self.ndim = 3
        self.shape = (count, 2, 2)

    def __getitem__(self, selection):
        assert selection == (slice(None), 0, slice(None))
        return SimpleNamespace(tolist=lambda: [[3.0, 4.0] for _ in range(self.shape[0])])


class SessionFixture:
    def __init__(self, *, blocked=False, error=None):
        self.gate = ThreadGate(blocked=blocked)
        self.error = error
        self.termination_error = RuntimeError("ONNX protocol fixture: native run terminated")
        self.calls = []

    def run(self, outputs, inputs, run_options=None):
        assert outputs == ["last_hidden_state"]
        assert isinstance(run_options, RunOptionsFixture)
        self.calls.append(run_options)
        with self.gate.active():
            if self.error is not None:
                raise self.error
            if run_options.terminate:
                raise self.termination_error
            return [HiddenStateFixture(len(inputs["input_ids"]))]


def protocol_embedder(*, tokenizer=None, session=None, options_factory=RunOptionsFixture):
    options = []

    def new_options():
        value = options_factory()
        options.append(value)
        return value

    embedder = OnnxBGEEmbedder.__new__(OnnxBGEEmbedder)
    embedder.config = KnowledgeConfig(embedding_dimensions=2)
    embedder.dimensions = 2
    embedder._cls_id = 101
    embedder._input_names = {"input_ids": "tensor(int64)", "attention_mask": "tensor(int64)"}
    embedder._np = SimpleNamespace(
        asarray=lambda values, *, dtype: values, int64="int64", int32="int32"
    )
    embedder._tokenizer = tokenizer if tokenizer is not None else TokenizerFixture()
    embedder._session = session if session is not None else SessionFixture()
    embedder._run_options = new_options
    return embedder, options


def exception_chain(error):
    chain = []
    while error is not None and error not in chain:
        chain.append(error)
        error = error.__cause__ or error.__context__
    return chain


@pytest.mark.parametrize("interruption", ["cancel", "timeout", "task-sweep"])
def test_native_run_terminates_and_joins_even_after_repeated_cancellation(interruption) -> None:
    async def scenario():
        session = SessionFixture(blocked=True)
        embedder, options = protocol_embedder(session=session)
        encoding = asyncio.create_task(embedder.embed(("native lifecycle fixture",)))
        await wait_for_thread_event(session.gate.started)
        if interruption != "timeout":
            public_task = encoding
            if interruption == "task-sweep":
                for task in asyncio.all_tasks() - {asyncio.current_task()}:
                    task.cancel("event loop task shutdown")
            else:
                encoding.cancel("first cancellation")
        else:
            public_task = asyncio.create_task(asyncio.wait_for(encoding, timeout=0))
        try:
            await wait_for_thread_event(options[0].terminated)
            assert session.calls == [options[0]]
            assert not public_task.done()
            assert not session.gate.finished.is_set()
            encoding.cancel("repeated cancellation during native shutdown")
            await asyncio.sleep(0)
            assert not public_task.done()
            assert not session.gate.finished.is_set()
            session.gate.release.set()
            expected = asyncio.TimeoutError if interruption == "timeout" else asyncio.CancelledError
            with pytest.raises(expected) as caught:
                await public_task
            # Python 3.10 adds a CancelledError context when a Task forwards cancellation.
            assert session.termination_error in exception_chain(caught.value)
            assert session.gate.finished.is_set()
        finally:
            session.gate.release.set()
            await asyncio.gather(public_task, return_exceptions=True)
        assert await embedder.embed(("subsequent call",)) == ((0.6, 0.8),)
        assert len(options) == 2 and options[0] is not options[1]
        assert options[0].terminate and not options[1].terminate
        assert session.calls == options

    asyncio.run(scenario())


def test_cancelled_native_call_cannot_return_vectors_when_runtime_finishes_normally() -> None:
    class FinishingSession(SessionFixture):
        def run(self, outputs, inputs, run_options=None):
            assert outputs == ["last_hidden_state"]
            self.calls.append(run_options)
            run_options.terminate = True
            return [HiddenStateFixture(len(inputs["input_ids"]))]

    embedder, options = protocol_embedder(session=FinishingSession())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(embedder.embed(("completion race fixture",)))
    assert len(options) == 1 and options[0].terminate
    assert embedder._session.calls == options


def test_cancellation_while_tokenizing_does_not_start_native_inference() -> None:
    async def scenario():
        tokenizer = TokenizerFixture(blocked=True)
        embedder, options = protocol_embedder(tokenizer=tokenizer)
        encoding = asyncio.create_task(embedder.embed(("tokenizer lifecycle fixture",)))
        await wait_for_thread_event(tokenizer.gate.started)
        encoding.cancel()
        try:
            await wait_for_thread_event(options[0].terminated)
            assert not encoding.done()
            assert not embedder._session.calls
            tokenizer.gate.release.set()
            with pytest.raises(asyncio.CancelledError):
                await encoding
            assert tokenizer.gate.finished.is_set()
            assert not embedder._session.calls
        finally:
            tokenizer.gate.release.set()
            await asyncio.gather(encoding, return_exceptions=True)

    asyncio.run(scenario())


def test_cancelled_queued_worker_exits_without_tokenizing_or_running_inference() -> None:
    async def scenario():
        gate = ThreadGate(blocked=True)

        def occupy_executor():
            with gate.active():
                return None

        with ThreadPoolExecutor(max_workers=1) as executor:
            loop = asyncio.get_running_loop()
            loop.set_default_executor(executor)
            occupant = loop.run_in_executor(None, occupy_executor)
            await wait_for_thread_event(gate.started)
            embedder, options = protocol_embedder()
            encoding = asyncio.create_task(embedder.embed(("queued lifecycle fixture",)))
            await asyncio.sleep(0)
            encoding.cancel()
            try:
                await wait_for_thread_event(options[0].terminated)
                assert not encoding.done()
                gate.release.set()
                with pytest.raises(asyncio.CancelledError):
                    await encoding
                assert not embedder._tokenizer.calls
                assert not embedder._session.calls
            finally:
                gate.release.set()
                await asyncio.gather(occupant, encoding, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["tokenizer", "inference"])
def test_original_encoding_error_propagates_without_cancellation(stage) -> None:
    error = RuntimeError("original native failure")
    tokenizer = TokenizerFixture(error=error if stage == "tokenizer" else None)
    session = SessionFixture(error=error if stage == "inference" else None)
    embedder, options = protocol_embedder(tokenizer=tokenizer, session=session)
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(embedder.embed(("native error fixture",)))
    assert caught.value is error
    assert not options[0].terminate


def test_embedding_worker_preserves_request_context() -> None:
    request_scope = ContextVar("onnx_test_request_scope")
    observed = []

    class ScopedTokenizer(TokenizerFixture):
        def encode_batch(self, texts):
            observed.append(request_scope.get())
            return super().encode_batch(texts)

    async def scenario():
        request_scope.set("request-123")
        embedder, _ = protocol_embedder(tokenizer=ScopedTokenizer())
        assert await embedder.embed(("context fixture",)) == ((0.6, 0.8),)

    asyncio.run(scenario())
    assert observed == ["request-123"]


def test_termination_signal_failure_waits_for_worker_and_preserves_both_errors() -> None:
    signal_error = RuntimeError("RunOptions termination signal failed")
    native_error = RuntimeError("independent native failure")

    class BrokenRunOptions(RunOptionsFixture):
        @RunOptionsFixture.terminate.setter
        def terminate(self, value):
            self.terminated.set()
            raise signal_error

    async def scenario():
        session = SessionFixture(blocked=True, error=native_error)
        embedder, options = protocol_embedder(session=session, options_factory=BrokenRunOptions)
        encoding = asyncio.create_task(embedder.embed(("signal failure fixture",)))
        await wait_for_thread_event(session.gate.started)
        encoding.cancel()
        try:
            await wait_for_thread_event(options[0].terminated)
            assert not encoding.done()
            session.gate.release.set()
            with pytest.raises(RuntimeError) as caught:
                await encoding
            assert caught.value is signal_error
            assert caught.value.__cause__ is native_error
            assert session.gate.finished.is_set()
        finally:
            session.gate.release.set()
            await asyncio.gather(encoding, return_exceptions=True)

    asyncio.run(scenario())
