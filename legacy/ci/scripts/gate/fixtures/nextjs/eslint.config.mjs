import tseslint from "typescript-eslint";
export default [{ ignores: ["node_modules/**", ".next/**", "next-env.d.ts"] }, {
  files: ["**/*.ts", "**/*.tsx"],
  languageOptions: { parser: tseslint.parser, parserOptions: { ecmaFeatures: { jsx: true } }, globals: { process: "readonly" } },
  plugins: { "@typescript-eslint": tseslint.plugin },
  rules: { "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_" }] }
}];
