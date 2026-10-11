"use strict";
const sessions = new Map();
const requests = new Map();
const bridgePath = "/abyssbeacon-red-bridge";

function storageUrl(value) {
    try {
        const url = new URL(value);
        const approved = (url.hostname === "b2.civitai.com"
            && url.pathname.startsWith("/file/civitai-modelfiles/"))
            || url.hostname === "civitai-delivery-worker-prod.5ac0637cfd0766c97916cefa3764fbdf.r2.cloudflarestorage.com";
        return url.protocol === "https:" && approved
            && (!url.port || url.port === "443") && !url.username && !url.password;
    } catch (_) { return false; }
}
function selectedFile(value, session) {
    try {
        const url = new URL(value);
        return ["https://civitai.red", "https://civitai.com"].includes(url.origin)
            && url.pathname === `/api/download/models/${session.version_id}`
            && url.searchParams.getAll("fileId").length === 1
            && url.searchParams.get("fileId") === session.file_id;
    } catch (_) { return false; }
}
async function bridgeFetch(session, path, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10000);
    try {
        const response = await fetch(session.origin + bridgePath + path, {
            ...options, credentials: "omit", cache: "no-store",
            headers: {...(options.headers || {}), Authorization: `Bearer ${session.token}`},
            signal: controller.signal
        });
        if (!response.ok) throw new Error("AbyssBeacon did not accept the handoff");
        return await response.json();
    } finally { clearTimeout(timer); }
}

async function trace(session, event, details = {}, destination = "") {
    try {
        const permissions = await browser.permissions.getAll();
        if (!permissions.data_collection?.includes("technicalAndInteraction")) return;
    } catch (_) { return; }
    if (!session) return;
    const data = {event, extension: "0.2.0"};
    try {
        if (details.url) {
            const parsed = new URL(details.url);
            data.host = parsed.hostname;
            const version = parsed.pathname.match(/^\/api\/download\/models\/(\d+)$/)?.[1];
            const file = parsed.searchParams.get("fileId");
            if (version) data.observed_version = version;
            if (/^\d+$/.test(file || "")) data.observed_file = file;
            data.matched = selectedFile(details.url, session);
        }
        if (destination) {
            data.destination_host = new URL(destination).hostname;
            data.allowed_storage = storageUrl(destination);
        }
        if (details.requestId != null) data.request_id = String(details.requestId);
        if (details.tabId != null) data.tab_id = details.tabId;
        if (details.statusCode != null) data.status = details.statusCode;
        data.tracked = details.requestId != null && requests.has(details.requestId);
        if (typeof details.success === "boolean") data.success = details.success;
    } catch (_) {}
    // Only allow-listed fields reach the server; no URLs, cookies or tokens.
    bridgeFetch(session, "/diagnostic", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify(data)
    }).catch(() => {});
}
function traceSession(details) {
    return requests.get(details.requestId) || sessions.get(details.tabId)
        || [...sessions.values()].find(session => selectedFile(details.url, session));
}

browser.runtime.onMessage.addListener(async (message, sender) => {
    if (message?.type !== "connect-abyssbeacon" || sender.tab?.id == null || sender.frameId !== 0) return;
    const page = new URL(sender.url);
    if (page.protocol !== "http:" || page.hostname !== "127.0.0.1"
        || page.pathname !== bridgePath + "/start" || !page.port) return;
    const token = page.hash.slice(1);
    if (!/^[A-Za-z0-9_-]{40,60}$/.test(token)) throw new Error("Invalid session");
    const session = {origin: page.origin, token, tabId: sender.tab.id};
    const pending = await bridgeFetch(session, "/pending");
    Object.assign(session, pending);
    if (!/^\d+$/.test(session.version_id) || !/^\d+$/.test(session.file_id)
        || !selectedFile(session.target_url, session)) throw new Error("Invalid selected file");
    session.expires = Date.now() + Math.min(600, Number(pending.timeout_seconds) || 300) * 1000;
    sessions.set(sender.tab.id, session);
    trace(session, "paired", {tabId: sender.tab.id, url: session.target_url});
    await browser.tabs.update(sender.tab.id, {url: session.target_url});
    return {connected: true};
});

function handoff(session, url) {
    if (session.handoff) return session.handoff;
    trace(session, "handoff-start", {}, url);
    session.handoff = (async () => {
        let success = false;
        try {
            const result = await bridgeFetch(session, "/capture", {
                method: "POST", headers: {"Content-Type": "application/json"},
                body: JSON.stringify({url, version_id: session.version_id, file_id: session.file_id})
            });
            success = result.success === true;
        } catch (_) { /* Never log a signed storage URL. */ }
        trace(session, "handoff-result", {success});
        sessions.delete(session.tabId);
        for (const [id, pending] of requests) if (pending === session) requests.delete(id);
        setTimeout(() => {
            browser.tabs.update(session.tabId, {
                url: browser.runtime.getURL(success ? "handoff.html" : "failed.html")
            }).catch(() => {});
        }, 100);
        return {cancel: true};
    })();
    return session.handoff;
}

const downloadRoutes = [
    "https://civitai.red/api/download/models/*",
    "https://civitai.com/api/download/models/*"
];

browser.webRequest.onHeadersReceived.addListener(details => {
    const diagnosticLocation = (details.responseHeaders || []).find(header => header.name.toLowerCase() === "location")?.value || "";
    trace(traceSession(details), "headers", details, diagnosticLocation);
    const session = requests.get(details.requestId) || sessions.get(details.tabId);
    if (!session || session.expires <= Date.now() || !selectedFile(details.url, session)
        || details.statusCode < 300 || details.statusCode >= 400) return {};
    const location = (details.responseHeaders || []).find(
        header => header.name.toLowerCase() === "location"
    )?.value;
    if (!storageUrl(location)) return {};
    // Intercept the authorized redirect BEFORE Firefox converts navigation
    // into a download and loses the original tab/request association.
    session.storage = location;
    return handoff(session, location);
}, {urls: downloadRoutes}, ["blocking", "responseHeaders"]);

browser.webRequest.onBeforeRedirect.addListener(details => {
    trace(traceSession(details), "redirect", details, details.redirectUrl);
    const session = requests.get(details.requestId) || sessions.get(details.tabId);
    if (session && session.expires > Date.now() && selectedFile(details.url, session)
        && storageUrl(details.redirectUrl)) session.storage = details.redirectUrl;
}, {urls: downloadRoutes});

browser.webRequest.onBeforeRequest.addListener(details => {
    const diagnosticSession = traceSession(details) || [...sessions.values()].find(session => session.storage === details.url);
    trace(diagnosticSession, storageUrl(details.url) ? "storage" : "request", details);
    const session = sessions.get(details.tabId);
    if (session && session.expires > Date.now() && selectedFile(details.url, session)) {
        requests.set(details.requestId, session);
        return {};
    }
    // Cached redirects can reach this fallback without the header listener.
    // Match the exact authorized signed URL, rather than a tabId that may now
    // be -1 or a requestId Firefox may have replaced for the download.
    const selected = [...sessions.values()].find(pending =>
        pending.expires > Date.now() && pending.storage === details.url
    );
    if (!selected || !storageUrl(details.url)) return {};
    return handoff(selected, details.url);
}, {urls: [...downloadRoutes, "https://b2.civitai.com/file/civitai-modelfiles/*",
    "https://civitai-delivery-worker-prod.5ac0637cfd0766c97916cefa3764fbdf.r2.cloudflarestorage.com/*"]}, ["blocking"]);

browser.tabs.onRemoved.addListener(tabId => {
    sessions.delete(tabId);
    for (const [id, session] of requests) if (session.tabId === tabId) requests.delete(id);
});
setInterval(() => {
    for (const [tabId, session] of sessions) if (session.expires <= Date.now()) sessions.delete(tabId);
    for (const [id, session] of requests) if (session.expires <= Date.now()) requests.delete(id);
}, 30000);
