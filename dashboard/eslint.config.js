// Flat config (ESLint 9). CI installs the plugins with --no-save so linting is
// enforced without changing package.json dependency resolution.
import js from "@eslint/js"
import reactHooks from "eslint-plugin-react-hooks"
import reactRefresh from "eslint-plugin-react-refresh"
import tseslint from "typescript-eslint"

export default tseslint.config(
  {
    // Build output and dependencies. Linting dist/ produces thousands of
    // errors in generated code and nothing actionable.
    ignores: ["dist/**", "node_modules/**", "eslint.config.js"],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2020,
      // typescript-eslint's eslint-recommended layer should already disable
      // no-undef for TypeScript files, since the compiler does that job
      // better. This list is belt and braces: several of these were being
      // used before today with no declaration, and no CI run has ever been
      // observed on this repository to prove the rule is actually off.
      globals: {
        window: "readonly",
        document: "readonly",
        console: "readonly",
        navigator: "readonly",
        fetch: "readonly",
        Headers: "readonly",
        Request: "readonly",
        Response: "readonly",
        Event: "readonly",
        AbortController: "readonly",
        DOMException: "readonly",
        AudioContext: "readonly",
        HTMLElement: "readonly",
        WebSocket: "readonly",
        setTimeout: "readonly",
        clearTimeout: "readonly",
        setInterval: "readonly",
        clearInterval: "readonly",
        localStorage: "readonly",
        sessionStorage: "readonly",
      },
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "react-refresh/only-export-components": [
        "warn",
        { allowConstantExport: true },
      ],
      // The dashboard talks to a JSON API. Genuine `any` at the boundary is
      // normal; warn so it stays visible without blocking the build.
      "@typescript-eslint/no-explicit-any": "warn",
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
    },
  },
)
