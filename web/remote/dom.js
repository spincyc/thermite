/*
 * Shared safe DOM and text helpers for thermite web pages.
 * Constructs elements via DOM APIs only; strips control and bidi-override
 * characters from untrusted text before placing it into the document.
 */
(function (global) {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";
  const UNSAFE_TEXT = /[\u0000-\u001f\u007f-\u009f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/g;
  const BOOL_PROPS = {
    checked: true,
    disabled: true,
    hidden: true,
    multiple: true,
    open: true,
    readOnly: true,
    required: true,
    selected: true
  };

  function cleanText(value, maxLen) {
    let text = String(value == null ? "" : value).replace(UNSAFE_TEXT, "").trim();
    if (typeof maxLen === "number" && maxLen > 0 && text.length > maxLen) {
      text = text.slice(0, maxLen).trim();
    }
    return text;
  }

  function appendChild(parent, child, doc) {
    if (child == null || child === false || child === true) return;
    if (Array.isArray(child)) {
      for (let i = 0; i < child.length; i += 1) appendChild(parent, child[i], doc);
      return;
    }
    if (typeof child === "string" || typeof child === "number") {
      const text = String(child);
      if (doc && typeof doc.createTextNode === "function") {
        parent.appendChild(doc.createTextNode(text));
      } else if (typeof parent.append === "function") {
        parent.append(text);
      } else {
        parent.textContent = (parent.textContent || "") + text;
      }
      return;
    }
    parent.appendChild(child);
  }

  function el(tag, attrs, ...children) {
    const doc = global.document;
    const node = doc.createElement(tag);
    if (attrs && typeof attrs === "object") {
      const keys = Object.keys(attrs);
      for (let i = 0; i < keys.length; i += 1) {
        const key = keys[i];
        const value = attrs[key];
        if (value === null || value === undefined || value === false) continue;
        if (key === "text" || key === "textContent") {
          node.textContent = String(value);
        } else if (key === "className") {
          node.className = String(value);
        } else if (key === "dataset" && typeof value === "object") {
          Object.keys(value).forEach((name) => {
            if (value[name] !== null && value[name] !== undefined) node.dataset[name] = String(value[name]);
          });
        } else if (BOOL_PROPS[key] && value === true) {
          node[key] = true;
          node.setAttribute(key, "");
        } else if (key === "value" && "value" in node) {
          node.value = String(value);
          node.setAttribute("value", String(value));
        } else if (value === true) {
          node.setAttribute(key, "");
        } else {
          node.setAttribute(key, String(value));
        }
      }
    }
    if (children.length === 1 && (typeof children[0] === "string" || typeof children[0] === "number")
        && !(attrs && ("text" in attrs || "textContent" in attrs))) {
      node.textContent = String(children[0]);
    } else {
      for (let i = 0; i < children.length; i += 1) appendChild(node, children[i], doc);
    }
    return node;
  }

  function svgIcon(className, symbol, ariaLabel) {
    const doc = global.document;
    const svg = doc.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", className || "icon");
    svg.setAttribute("focusable", "false");
    if (ariaLabel) {
      svg.setAttribute("role", "img");
      svg.setAttribute("aria-label", String(ariaLabel));
    } else {
      svg.setAttribute("aria-hidden", "true");
    }
    const use = doc.createElementNS(SVG_NS, "use");
    const ref = String(symbol || "");
    use.setAttribute("href", ref.charAt(0) === "#" ? ref : "#" + ref);
    svg.appendChild(use);
    return svg;
  }

  const api = { cleanText, el, svgIcon, SVG_NS };
  global.ThermiteDom = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(globalThis);
