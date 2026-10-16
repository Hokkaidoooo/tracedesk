export function element<K extends keyof HTMLElementTagNameMap>(tag: K, className?: string, text?: string): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export function required<T extends HTMLElement>(selector: string): T {
  const node = document.querySelector<T>(selector);
  if (!node) throw new Error(`缺少页面元素：${selector}`);
  return node;
}

export function button(label: string, className: string, onClick: () => void): HTMLButtonElement {
  const node = element('button', className, label);
  node.type = 'button';
  node.addEventListener('click', onClick);
  return node;
}

export function definition(label: string, value: string): HTMLDivElement {
  const row = element('div', 'definition-row');
  row.append(element('dt', undefined, label), element('dd', undefined, value));
  return row;
}

export function money(amountMinor: number, currency: string): string {
  return new Intl.NumberFormat('zh-CN', { style: 'currency', currency }).format(amountMinor / 100);
}

export function timestamp(value: string, timeOnly = false): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return `无效时间：${value}`;
  return parsed.toLocaleString('zh-CN', timeOnly
    ? { hour: '2-digit', minute: '2-digit', hour12: false }
    : { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
}

export function runtimeLabel(runtime: 'extractive' | 'llm'): string {
  return runtime === 'extractive' ? '提取式模式' : '模型模式';
}
