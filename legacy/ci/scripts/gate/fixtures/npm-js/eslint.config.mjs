export default [{ ignores: ["node_modules/**", "dist/**", ".next/**"] }, {
  files: ["**/*.js", "**/*.mjs"],
  languageOptions: { ecmaVersion: 2022, sourceType: "module", globals: { process: "readonly" } },
  rules: { "no-unused-vars": ["error", { argsIgnorePattern: "^_" }], "no-undef": "error" }
}];
