// ESLint configuration — AAH scaffold default (flat config).
//
// Deliberately imports nothing: eslint's own recommended config would need
// `@eslint/js`, and a missing import makes eslint fail to load rather than
// report findings — which the standards gate must treat as "could not check".
// Extend this file (do not replace it) when the package needs real plugins;
// add the plugin to devDependencies in the same change.

export default [
  {
    ignores: [
      ".aah/**",
      ".claude/**",
      "node_modules/**",
      "dist/**",
      "build/**",
      "coverage/**",
    ],
  },
  {
    files: ["**/*.{js,mjs,cjs,jsx,ts,tsx}"],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
    },
    rules: {
      "no-unused-vars": "warn",
      "no-undef": "off",
    },
  },
];
