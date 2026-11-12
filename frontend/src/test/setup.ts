import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => cleanup());

// jsdom has no layout: scrolling is a no-op.
Element.prototype.scrollIntoView = function scrollIntoView() {};

// Radix Select uses pointer capture, which jsdom lacks.
Element.prototype.hasPointerCapture = function hasPointerCapture() { return false; };
Element.prototype.releasePointerCapture = function releasePointerCapture() {};

// Radix Checkbox measures itself with ResizeObserver, which jsdom lacks.
globalThis.ResizeObserver ??= class ResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
};
