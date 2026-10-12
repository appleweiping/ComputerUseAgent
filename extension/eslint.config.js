"use strict";

// ESLint flat config for the extension package (CommonJS package: the unit
// tests are CommonJS .js files; ES modules use the .mjs extension).
const js = require("@eslint/js");
const globals = require("globals");

const sharedRules = {
  "no-unused-vars": [
    "error",
    {
      argsIgnorePattern: "^_",
      varsIgnorePattern: "^_",
      caughtErrorsIgnorePattern: "^_",
      destructuredArrayIgnorePattern: "^_"
    }
  ],
  "no-empty": ["error", { allowEmptyCatch: true }],
  eqeqeq: ["error", "always", { null: "ignore" }],
  "no-var": "error",
  "prefer-const": "error"
};

// Extension pages and scripts that run inside Chrome.
const extensionGlobals = {
  ...globals.browser,
  ...globals.webextensions,
  CUIRecorderCore: "readonly"
};

module.exports = [
  {
    ignores: ["node_modules/**", "coverage/**", ".e2e-results/**"]
  },
  js.configs.recommended,
  {
    files: ["**/*.js", "**/*.mjs"],
    languageOptions: { ecmaVersion: 2024 },
    rules: sharedRules
  },
  {
    // UMD core shared by the service worker, pages, content script, playground and Node tests.
    files: ["app/src/core.js"],
    languageOptions: {
      sourceType: "script",
      globals: { ...globals.browser, module: "readonly" }
    }
  },
  {
    files: ["app/src/background.js"],
    languageOptions: {
      sourceType: "script",
      globals: { ...globals.serviceworker, ...globals.webextensions, CUIRecorderCore: "readonly" }
    }
  },
  {
    files: ["app/src/content.js", "app/popup/**/*.js", "app/dashboard/**/*.js", "playground/**/*.js"],
    languageOptions: { sourceType: "script", globals: extensionGlobals }
  },
  {
    files: ["eslint.config.js", "test/**/*.js"],
    languageOptions: { sourceType: "commonjs", globals: { ...globals.node } }
  },
  {
    // E2E code runs in Node, but functions passed to page.evaluate() run in the
    // extension or page context.
    files: ["test/**/*.mjs", "scripts/**/*.mjs"],
    languageOptions: {
      sourceType: "module",
      globals: { ...globals.node, ...extensionGlobals }
    }
  }
];
