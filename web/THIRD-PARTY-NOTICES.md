# Web component inventory

Own HTML, CSS, JavaScript and build tooling are covered by the repository Apache-2.0 license.

Three.js 0.186.1 is installed from the exact version and integrity in package-lock.json. It is licensed under MIT (copyright 2010-2026 Three.js authors). Its unmodified LICENSE is included at web/THREE-LICENSE.txt and in each build at vendor/THREE-LICENSE.txt.

The build exports only build/three.module.js, build/three.core.js and examples/jsm/controls/OrbitControls.js. Relative import paths are rewritten for same-origin loading. No other npm source or runtime dependency is shipped. Fonts and application dependencies are served locally. Yandex Metrika loads its remote counter script for visits and fixed action goals; Webvisor, click maps, automatic link tracking and page titles are disabled. Pinned local font binaries and their licenses are described below.

## Axis Sites brand and local fonts

The dark/light theme tokens, official Axis sign and typographic choices follow the existing Axis Sites public design system (axisplatform.ru). The sign is used with the owner's authorization; its SHA256 and source inventory are in assets/inventory.json. No other site's code, analytics, screenshots, credentials or customer data are included.

Six unmodified WOFF2 files are vendored from Fontsource packages @fontsource-variable/manrope 5.3.0 and @fontsource/geologica 5.3.0 already present in Axis Sites: Latin/Cyrillic Manrope variable weights 200–800 and Latin/Cyrillic Geologica 700/800. Their bytes and SHA256 are recorded in assets/inventory.json. Manrope is copyright 2019 The Manrope Project Authors; Geologica is copyright 2020 The Geologica Project Authors. Both are SIL Open Font License 1.1; complete licenses are included under assets/fonts/MANROPE-OFL.txt and assets/fonts/GEOLOGICA-OFL.txt and copied into each build. Font files are served only from the same origin and require no npm dependency or external font request.
