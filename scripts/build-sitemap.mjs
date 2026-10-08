#!/usr/bin/env node
/**
 * Карта сайта из фактического артефакта сборки. Один инструмент на все сайты.
 *
 * Раньше каждый сайт вёл её по-своему: axisconsult собирал в своём скрипте
 * раскладки, axisplatform держал написанный руками файл в public. Итог
 * предсказуемый: в карте платформы висела служебная страница /privacy,
 * которую индексировать не нужно, а при добавлении маршрута правку надо было
 * помнить в двух местах. Карта, которую ведут руками, расходится с сайтом
 * молча, и заметить это некому.
 *
 * Источник истины теперь один и тот же для обоих сайтов — то, что реально
 * собралось:
 *
 *   static  маршруты берутся из готовых index.html в артефакте, признак
 *           индексации из метатега robots в самой странице;
 *   app     маршруты берутся из файлов src/routes приложения, признак
 *           индексации из наличия noindex в исходнике маршрута.
 *
 * В карту идут только индексируемые адреса. Языковые версии получают блок
 * hreflang, он читается из готовой разметки главной страницы, а не
 * задаётся отдельно: разойтись с ней нечему.
 *
 * Запуск из каталога сайта:
 *   статика, после сборки:
 *     node ../_deploy/sitemap.mjs --domain https://axisconsult.ru --kind static --artifact dist
 *   приложение, до сборки:
 *     node ../_deploy/sitemap.mjs --domain https://axisplatform.ru --kind app --artifact public
 */

import { readdir, readFile, writeFile, mkdir, stat } from "node:fs/promises";
import { existsSync } from "node:fs";
import { join, resolve } from "node:path";

function arg(name, fallback = null) {
  const i = process.argv.indexOf(`--${name}`);
  if (i !== -1 && process.argv[i + 1]) return process.argv[i + 1];
  if (fallback !== null) return fallback;
  throw new Error(`не задан обязательный параметр --${name}`);
}

const DOMAIN = arg("domain").replace(/\/$/, "");
const KIND = arg("kind");
const ARTIFACT = resolve(arg("artifact"));
const STAMP = new Date().toISOString().slice(0, 10);

/** Приоритет и частота обхода по типу страницы, а не по ручному списку. */
function weight(route) {
  if (route === "/") return { priority: "1.0", changefreq: "weekly" };
  // Языковые версии главной равноценны ей самой.
  if (/^\/[a-z]{2}$/.test(route)) return { priority: "1.0", changefreq: "weekly" };
  return { priority: "0.6", changefreq: "monthly" };
}

/** Маршруты собранной статики: каждый index.html это адрес. */
async function staticRoutes(dir, prefix = "") {
  const found = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    // .routes это служебная раскладка для хранилища, не адреса сайта.
    if (entry.isDirectory() && !entry.name.startsWith(".") && entry.name !== "assets") {
      found.push(...(await staticRoutes(join(dir, entry.name), `${prefix}/${entry.name}`)));
    }
    if (entry.isFile() && entry.name === "index.html") {
      const html = await readFile(join(dir, entry.name), "utf8");
      found.push({ route: prefix === "" ? "/" : prefix, html });
    }
  }
  return found;
}

/** Маршруты приложения на файловой маршрутизации. */
async function appRoutes(siteDir) {
  const dir = join(siteDir, "src", "routes");
  const found = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    if (!entry.isFile() || !entry.name.endsWith(".tsx")) continue;
    if (entry.name.startsWith("__") || entry.name.startsWith("_")) continue;
    const source = await readFile(join(dir, entry.name), "utf8");
    const name = entry.name.replace(/\.tsx$/, "");
    found.push({ route: name === "index" ? "/" : `/${name}`, html: source });
  }
  return found;
}

/** Страница закрыта от индексации. Проверяется по самой странице. */
function noindex(content) {
  return /content=["'][^"']*noindex/i.test(content) || /"noindex/i.test(content);
}

/** Блок языковых версий. Читается из разметки главной, если он там есть. */
function alternates(pages) {
  const home = pages.find((p) => p.route === "/");
  if (!home) return "";
  const links = [...home.html.matchAll(/hreflang="([^"]+)"\s+href="([^"]+)"/g)];
  if (links.length === 0) return "";
  return links
    .map(([, lang, href]) => `    <xhtml:link rel="alternate" hreflang="${lang}" href="${href}" />`)
    .join("\n");
}

async function main() {
  const siteDir = process.cwd();
  const pages = KIND === "static" ? await staticRoutes(ARTIFACT) : await appRoutes(siteDir);

  if (pages.length === 0) {
    throw new Error(`в артефакте ${ARTIFACT} не найдено ни одного маршрута`);
  }

  const indexable = pages.filter((p) => !noindex(p.html)).sort((a, b) => a.route.localeCompare(b.route));
  const skipped = pages.filter((p) => noindex(p.html)).map((p) => p.route);

  if (indexable.length === 0) {
    throw new Error("все маршруты закрыты от индексации, карта сайта пуста");
  }

  // Индексируемая страница обязана называть себя канонической. Без этого
  // поисковик склеит её с другой копией по своему усмотрению.
  if (KIND === "static") {
    for (const page of indexable) {
      if (!page.html.includes('rel="canonical"')) {
        throw new Error(`страница ${page.route} без canonical, в карту не пойдёт`);
      }
    }
  }

  const alt = alternates(pages);
  const body = indexable
    .map((page) => {
      const { priority, changefreq } = weight(page.route);
      const loc = page.route === "/" ? `${DOMAIN}/` : `${DOMAIN}${page.route}`;
      const langBlock = alt && /^\/([a-z]{2})?$/.test(page.route) ? `${alt}\n` : "";
      return (
        `  <url>\n    <loc>${loc}</loc>\n    <lastmod>${STAMP}</lastmod>\n` +
        `    <changefreq>${changefreq}</changefreq>\n    <priority>${priority}</priority>\n` +
        langBlock +
        `  </url>`
      );
    })
    .join("\n");

  const xml =
    `<?xml version="1.0" encoding="UTF-8"?>\n` +
    `<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"\n` +
    `        xmlns:xhtml="http://www.w3.org/1999/xhtml">\n${body}\n</urlset>\n`;

  // Статика: файл кладётся прямо в собранный каталог, его отдаёт общий сервер.
  // Приложение: файл кладётся в public ДО сборки. Nitro запекает перечень
  // публичных файлов в момент сборки, и всё, что появилось позже, сервер не
  // отдаёт: проверено на живом контуре, адрес возвращал 404 при файле,
  // лежащем внутри образа. Поэтому для приложения карта собирается из
  // исходных маршрутов первым шагом сборки и попадает в репозиторий.
  const target = ARTIFACT;
  if (!existsSync(target)) await mkdir(target, { recursive: true });
  await writeFile(join(target, "sitemap.xml"), xml, "utf8");

  console.log(`  карта сайта: ${indexable.length} адресов (${indexable.map((p) => p.route).join(", ")})`);
  if (skipped.length) console.log(`  закрыты от индексации: ${skipped.join(", ")}`);
}

main().catch((error) => {
  console.error("Сборка карты сайта не удалась:", error.message);
  process.exit(1);
});
