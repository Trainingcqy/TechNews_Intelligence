const DATE_TIME = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false
});
const DATE = new Intl.DateTimeFormat("sv-SE", { timeZone: "Asia/Shanghai" });

export function formatMs(ms) {
  if (ms === null || ms === undefined) return "-";
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}

export function formatTime(iso) {
  return iso ? DATE_TIME.format(new Date(iso)).replace("/", "-") : "-";
}

export function formatDate(iso) {
  return iso ? DATE.format(new Date(iso)) : "";
}

export function formatCount(value) {
  return value === null || value === undefined ? "-" : Number(value).toLocaleString("en-US");
}

export function formatScore(value) {
  return value === null || value === undefined ? "-" : Number(value).toFixed(3);
}

export function shortId(id) {
  return id ? String(id).slice(0, 8) : "";
}

export function compactArgs(args) {
  return Object.entries(args || {})
    .filter(([, value]) => value !== null && value !== undefined && value !== "" && !(Array.isArray(value) && !value.length))
    .map(([key, value]) => `${key}=${Array.isArray(value) ? value.join(",") : value}`)
    .join("  ");
}
