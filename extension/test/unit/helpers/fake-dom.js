// Minimal DOM stand-in for running popup.js and dashboard.js under node:test.
// It implements only what those pages use: element properties, listeners with
// bubbling, a tiny selector matcher, fragments, and child-list operations.

const SELECTOR = /^([a-z0-9]+)?(?:#([\w-]+))?((?:\.[\w-]+)*)(?:\[([\w-]+)(?:="([^"]*)")?\])?$/i;

function datasetKey(attribute) {
  return attribute.replace(/^data-/, "").replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
}

function matches(element, selector) {
  if (!element || element.isFragment) return false;
  const parsed = SELECTOR.exec(selector.trim());
  if (!parsed) throw new Error(`fake-dom does not support selector ${selector}`);
  const [, tag, id, classes, attribute, attributeValue] = parsed;
  if (tag && element.tagName !== tag.toUpperCase()) return false;
  if (id && element.id !== id) return false;
  for (const name of classes.split(".").filter(Boolean)) {
    if (!element.classList.contains(name)) return false;
  }
  if (attribute) {
    const value = attribute.startsWith("data-")
      ? element.dataset[datasetKey(attribute)]
      : element.attributes[attribute];
    if (value === undefined || value === null) return false;
    if (attributeValue !== undefined && String(value) !== attributeValue) return false;
  }
  return true;
}

class FakeNode {
  constructor() {
    this.children = [];
    this.parentNode = null;
  }

  appendChild(child) {
    if (child.isFragment) {
      for (const nested of [...child.children]) this.appendChild(nested);
      child.children = [];
      return child;
    }
    child.remove?.();
    child.parentNode = this;
    this.children.push(child);
    return child;
  }

  append(...children) {
    for (const child of children) this.appendChild(child);
  }

  replaceChildren(...children) {
    for (const child of this.children) child.parentNode = null;
    this.children = [];
    this.append(...children);
  }

  *descendants() {
    for (const child of this.children) {
      yield child;
      yield* child.descendants();
    }
  }

  querySelector(selector) {
    for (const node of this.descendants()) if (matches(node, selector)) return node;
    return null;
  }

  querySelectorAll(selector) {
    return [...this.descendants()].filter((node) => matches(node, selector));
  }
}

class FakeElement extends FakeNode {
  constructor(tagName = "div", options = {}) {
    super();
    this.tagName = String(tagName).toUpperCase();
    this.id = options.id || "";
    this.className = options.className || "";
    this.dataset = { ...(options.dataset || {}) };
    this.attributes = { ...(options.attributes || {}) };
    this.hidden = Boolean(options.hidden);
    this.value = options.value ?? "";
    this.checked = Boolean(options.checked);
    this.disabled = false;
    this.style = {};
    this.listeners = new Map();
    this._text = options.text ?? "";
    this.clicks = 0;
  }

  get textContent() {
    return this.children.length ? this.children.map((child) => child.textContent).join("") : this._text;
  }

  set textContent(value) {
    this.replaceChildren();
    this._text = String(value);
  }

  get classList() {
    const element = this;
    const tokens = () => element.className.split(/\s+/).filter(Boolean);
    return {
      add(name) { if (!tokens().includes(name)) element.className = [...tokens(), name].join(" "); },
      remove(name) { element.className = tokens().filter((token) => token !== name).join(" "); },
      contains(name) { return tokens().includes(name); }
    };
  }

  addEventListener(type, listener) {
    const rows = this.listeners.get(type) || [];
    rows.push(listener);
    this.listeners.set(type, rows);
  }

  // Dispatch with bubbling; resolves after every (possibly async) listener settles.
  async dispatch(type, init = {}) {
    const event = { type, target: this, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...init };
    const pending = [];
    for (let node = this; node; node = node.parentNode) {
      for (const listener of node.listeners?.get(type) || []) pending.push(listener(event));
    }
    await Promise.all(pending);
    return event;
  }

  click() {
    this.clicks += 1;
    return this.dispatch("click");
  }

  remove() {
    if (!this.parentNode) return;
    this.parentNode.children = this.parentNode.children.filter((child) => child !== this);
    this.parentNode = null;
  }

  closest(selector) {
    for (let node = this; node && !node.isFragment; node = node.parentNode) {
      if (matches(node, selector)) return node;
    }
    return null;
  }

  getAttribute(name) {
    return this.attributes[name] ?? null;
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }
}

class FakeFragment extends FakeNode {
  constructor(children = []) {
    super();
    this.isFragment = true;
    for (const child of children) this.appendChild(child);
  }
}

function el(tagName, options = {}, children = []) {
  const element = new FakeElement(tagName, options);
  element.append(...children);
  return element;
}

module.exports = { FakeElement, FakeFragment, el, matches };
