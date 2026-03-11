#!/usr/bin/env node
const fs = require("node:fs");
const path = require("node:path");

function merge(baseValue, overlayValue) {
  if (Array.isArray(overlayValue)) {
    return overlayValue.slice();
  }
  if (overlayValue && typeof overlayValue === "object") {
    const baseObject = baseValue && typeof baseValue === "object" && !Array.isArray(baseValue)
      ? baseValue
      : {};
    const merged = { ...baseObject };
    for (const [key, value] of Object.entries(overlayValue)) {
      merged[key] = merge(baseObject[key], value);
    }
    return merged;
  }
  return overlayValue;
}

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}

const target = String(process.argv[2] || "").trim().toLowerCase();
if (target !== "chrome" && target !== "firefox") {
  console.error("Usage: node build-manifest.js <chrome|firefox>");
  process.exit(1);
}

const rootDir = __dirname;
const basePath = path.join(rootDir, "manifest.base.json");
const overlayPath = path.join(rootDir, `manifest.${target}.json`);
const outPath = path.join(rootDir, "dist", target, "manifest.json");

const baseManifest = readJson(basePath);
const overlayManifest = readJson(overlayPath);
const mergedManifest = merge(baseManifest, overlayManifest);

fs.mkdirSync(path.dirname(outPath), { recursive: true });
fs.writeFileSync(outPath, `${JSON.stringify(mergedManifest, null, 2)}\n`, "utf8");

console.log(`Wrote ${outPath}`);
