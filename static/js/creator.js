function initializeCreatorPage() {
    const statusBox = document.getElementById("creatorScanStatus");
    const message = document.getElementById("creatorScanMessage");
    const counts = document.getElementById("creatorScanCounts");
    const creator = window.MODELRADAR_CREATOR;
    const creatorScanOpen = document.getElementById("openCreatorScanBtn");
    const creatorScanDetailsButton = document.getElementById("creatorScanDetailsButton");
    const creatorScanDialog = document.getElementById("creatorScanDialog");
    const creatorScanSubmit = document.getElementById("creatorScanSubmit");
    const targetedOptions = document.getElementById("creatorTargetedOptions");
    const architecture = document.getElementById("creatorArchitecture");
    const modelType = document.getElementById("creatorModelType");

    initializeModal();
    initializeFullscreen();
    if (!creator) return;

    let polling = null;
    let running = false;
    let creatorScanStartedAt = null;
    let elapsedTimer = null;
    let lastStatusData = {};
    let creatorDetailsOpen = false;

    const sourceCheckButton = document.getElementById("checkCreatorSources");

    async function checkCreatorSources({showBusy = true} = {}) {
        if (sourceCheckButton && showBusy) {
            sourceCheckButton.disabled = true;
            sourceCheckButton.textContent = "Checking…";
        }

        if (showBusy) {
            document.querySelectorAll(".creator-source-check-row .creator-source-status").forEach(el => {
                el.textContent = "Checking…";
                el.className = "creator-source-status";
            });
        }

        try {
            const response = await fetch(`/creator/${encodeURIComponent(creator)}/sources/check`, {cache: "no-store"});
            const data = await response.json();
            if (!response.ok || !data.success) throw new Error(data.error || "Source check failed.");

            Object.entries(data.results || {}).forEach(([source, result]) => {
                const row = document.querySelector(`.creator-source-check-row[data-source="${source}"]`);
                if (!row) return;
                const known = row.querySelector(".creator-source-known");
                const status = row.querySelector(".creator-source-status");
                if (known) known.textContent = `${Number(result.stored) || 0} stored`;
                if (!status) return;
                status.className = "creator-source-status";
                if (result.status === "found") {
                    status.textContent = "Exact username found";
                    status.classList.add("found");
                } else if (result.status === "stored") {
                    status.textContent = "Already found in AbyssBeacon";
                    status.classList.add("found");
                } else if (result.status === "known_identity") {
                    status.textContent = "Known creator account";
                    status.classList.add("found");
                } else if (result.status === "unsupported") {
                    status.textContent = "Creator account not learned yet";
                } else if (result.status === "not_found") {
                    status.textContent = "No public models found";
                    status.classList.add("not-found");
                } else {
                    status.textContent = result.error ? `Check failed · ${result.error}` : "Check failed";
                    status.classList.add("error");
                }
            });

            return data.results || {};
        } catch (error) {
            if (showBusy) {
                document.querySelectorAll(".creator-source-check-row .creator-source-status").forEach(el => {
                    el.textContent = error.message || "Check failed";
                    el.className = "creator-source-status error";
                });
            }
            throw error;
        } finally {
            if (sourceCheckButton && showBusy) {
                sourceCheckButton.disabled = false;
                sourceCheckButton.textContent = "Check Sources";
            }
        }
    }

    if (sourceCheckButton) {
        sourceCheckButton.addEventListener("click", () => {
            checkCreatorSources({showBusy: true}).catch(() => {});
        });
    }

    function targetedSources() {
        return Array.from(document.querySelectorAll("#creatorSourceSelect input:checked"))
            .map(input => input.value);
    }

    function allEnabledSources() {
        const configured = Array.isArray(window.MODELRADAR_ENABLED_CREATOR_SOURCES)
            ? window.MODELRADAR_ENABLED_CREATOR_SOURCES.filter(Boolean)
            : [];
        if (configured.length) return configured;
        return Array.from(document.querySelectorAll(".creator-source-pill[data-source]"))
            .map(el => el.dataset.source)
            .filter(Boolean);
    }

    function enabledScanArchitectures() {
        const configured = Array.isArray(window.userPreferences?.scan_architectures)
            ? window.userPreferences.scan_architectures
                .map(value => String(value || "").trim())
                .filter(Boolean)
            : [];

        if (configured.length) return configured;

        // Same fallback as the main SCAN UI: when no explicit preference has
        // been saved yet, every configured architecture is considered enabled.
        return Array.from(document.querySelectorAll("#creatorArchitecture option"))
            .map(option => String(option.value || "").trim())
            .filter(value => value && value !== "Other");
    }

    async function matchingSources() {
        const enabled = new Set(allEnabledSources());
        const results = await checkCreatorSources({showBusy: true});
        return Object.entries(results || {})
            .filter(([source, result]) => {
                if (!enabled.has(source)) return false;
                const stored = Number(result?.stored) || 0;
                return stored > 0 || result?.status === "found" || result?.status === "stored" || result?.status === "known_identity";
            })
            .map(([source]) => source);
    }

    function formatElapsed(ms) {
        const totalSeconds = Math.max(0, Math.floor(Number(ms || 0) / 1000));
        const minutes = Math.floor(totalSeconds / 60);
        const seconds = totalSeconds % 60;
        if (minutes >= 60) {
            const hours = Math.floor(minutes / 60);
            const remainingMinutes = minutes % 60;
            return `${hours}:${String(remainingMinutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
        }
        return `${minutes}:${String(seconds).padStart(2, "0")}`;
    }

    function renderCreatorScanCounts(data = lastStatusData) {
        if (!counts) return;

        const bits = [];
        const source = String(data?.source || "").trim();
        if (source) bits.push(source);

        if (Number(data?.processed) > 0) bits.push(`${Number(data.processed)} processed`);
        if (Number(data?.added) > 0) bits.push(`${Number(data.added)} new`);
        if (Number(data?.updated) > 0) bits.push(`${Number(data.updated)} updated`);

        if (creatorScanStartedAt) {
            bits.push(`${formatElapsed(Date.now() - creatorScanStartedAt)} elapsed`);
        }

        counts.textContent = bits.join(" · ");
    }

    function startElapsedTimer() {
        if (!creatorScanStartedAt) creatorScanStartedAt = Date.now();
        if (elapsedTimer) clearInterval(elapsedTimer);
        elapsedTimer = setInterval(() => renderCreatorScanCounts(), 1000);
        renderCreatorScanCounts();
    }

    function stopElapsedTimer() {
        if (elapsedTimer) clearInterval(elapsedTimer);
        elapsedTimer = null;
    }

    function setCreatorDetailsOpen(open) {
        creatorDetailsOpen = Boolean(open);
        if (statusBox) statusBox.hidden = !creatorDetailsOpen;
        creatorScanDetailsButton?.classList.toggle("active", creatorDetailsOpen);
    }

    creatorScanDetailsButton?.addEventListener("click", () => {
        setCreatorDetailsOpen(!creatorDetailsOpen);
        if (creatorDetailsOpen && running) poll();
    });

    function setRunning(nextRunning) {
        running = nextRunning;
        [creatorScanOpen, creatorScanSubmit].forEach(btn => {
            if (btn) btn.disabled = nextRunning;
        });

        creatorScanDetailsButton?.classList.toggle("is-running", nextRunning);
        creatorScanDetailsButton?.setAttribute(
            "title",
            nextRunning
                ? "Creator Scan is running. Show or hide live progress."
                : "Show or hide Creator Scan progress"
        );

        if (nextRunning) startElapsedTimer();
        else stopElapsedTimer();
    }

    async function poll() {
        try {
            const response = await fetch("/scan/status", {cache: "no-store"});
            const data = await response.json();
            lastStatusData = data || {};
            if (message) message.textContent = data.message || "Scanning creator…";
            renderCreatorScanCounts(data);
            if (data.status && data.status !== "running") {
                clearInterval(polling);
                polling = null;
                renderCreatorScanCounts(data);
                setRunning(false);
                setCreatorDetailsOpen(true);
                if (message) message.textContent = data.message || "Creator scan complete.";
                setTimeout(() => window.location.reload(), 1200);
            }
        } catch (error) {
            console.error("Creator scan status error:", error);
        }
    }

    async function startCreatorScan({
        mode,
        sources,
        architectureValue = "",
        architectureValues = [],
        modelTypeValue = ""
    }) {
        if (running) return;
        if (!sources.length) {
            window.alert("Select at least one source to scan.");
            return;
        }
        if (mode === "targeted" && !architectureValue && !modelTypeValue) {
            window.alert("Choose an architecture or model type, or use Scan Everything.");
            return;
        }

        creatorScanDialog?.close();
        creatorScanStartedAt = Date.now();
        lastStatusData = {};
        setRunning(true);
        setCreatorDetailsOpen(true);

        const scope = mode === "everything"
            ? "everything"
            : mode === "matching"
                ? `${architectureValues.length || 0} enabled architecture${architectureValues.length === 1 ? "" : "s"}`
                : [architectureValue, modelTypeValue].filter(Boolean).join(" · ");
        if (message) message.textContent = `Starting creator scan for ${creator}…`;
        lastStatusData = {source: sources.length === 1 ? sources[0] : ""};
        if (counts) {
            counts.textContent = `${scope} · ${formatElapsed(0)} elapsed`;
        }

        try {
            const response = await fetch(`/creator/${encodeURIComponent(creator)}/scan`, {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({
                    sources,
                    mode,
                    architecture: architectureValue,
                    architectures: architectureValues,
                    model_type: modelTypeValue
                })
            });
            const data = await response.json();
            if (!response.ok || !data.success) throw new Error(data.error || "Unable to start creator scan.");
            await poll();
            polling = setInterval(poll, 900);
        } catch (error) {
            setRunning(false);
            if (statusBox) statusBox.hidden = false;
            if (message) message.textContent = error.message;
        }
    }

    function resetCreatorScanDialog() {
        const everything = document.querySelector('input[name="creatorDialogScanMode"][value="everything"]');
        if (everything) everything.checked = true;
        if (targetedOptions) targetedOptions.hidden = true;
        if (architecture) architecture.value = "";
        if (modelType) modelType.value = "";
        document.querySelectorAll("#creatorSourceSelect input[type=checkbox]").forEach(input => input.checked = true);
    }

    async function restoreRunningCreatorScan() {
        try {
            const response = await fetch("/scan/status", {cache: "no-store"});
            const data = await response.json();
            if (data?.status !== "running") return;

            const current = String(data?.current || "").trim().casefold();
            const statusMessage = String(data?.message || "").casefold();
            const creatorName = String(creator || "").trim().casefold();

            // Do not display some unrelated global/source scan as this creator's
            // scan. Creator scans identify the creator in `current` once a source
            // starts, and in the initial status message before that.
            const belongsHere =
                (current && current === creatorName) ||
                statusMessage.includes(`creator ${creatorName}`);

            if (!belongsHere) return;

            creatorScanStartedAt = Date.now();
            lastStatusData = data || {};
            setRunning(true);
            setCreatorDetailsOpen(true);
            if (message) message.textContent = data.message || `Scanning creator ${creator}…`;
            renderCreatorScanCounts(data);

            await poll();
            if (!polling && running) {
                polling = setInterval(poll, 900);
            }
        } catch (error) {
            console.debug("Unable to restore creator scan indicator:", error);
        }
    }

    restoreRunningCreatorScan();

    creatorScanOpen?.addEventListener("click", () => {
        resetCreatorScanDialog();
        creatorScanDialog?.showModal();
    });

    document.querySelectorAll('input[name="creatorDialogScanMode"]').forEach(input => {
        input.addEventListener("change", () => {
            if (targetedOptions) targetedOptions.hidden = input.value !== "targeted" || !input.checked;
        });
    });

    creatorScanSubmit?.addEventListener("click", async () => {
        const mode = document.querySelector('input[name="creatorDialogScanMode"]:checked')?.value || "everything";

        let sources = [];
        try {
            if (mode === "everything") {
                sources = allEnabledSources();
            } else if (mode === "matching") {
                creatorScanSubmit.disabled = true;
                creatorScanSubmit.textContent = "Checking Sources…";
                sources = await matchingSources();
                if (!sources.length) {
                    window.alert("No matching creator sources were found. Run Scan Everything, or use Targeted Scan to choose sources manually.");
                    return;
                }
            } else {
                sources = targetedSources();
            }

            await startCreatorScan({
                mode,
                sources,
                architectureValue: mode === "targeted" ? (architecture?.value || "") : "",
                architectureValues: mode === "matching" ? enabledScanArchitectures() : [],
                modelTypeValue: mode === "targeted" ? (modelType?.value || "") : ""
            });
        } catch (error) {
            window.alert(error.message || "Unable to check matching creator sources.");
        } finally {
            if (!running && creatorScanSubmit) {
                creatorScanSubmit.disabled = false;
                creatorScanSubmit.textContent = "Scan Creator";
            }
        }
    });
}

document.addEventListener("DOMContentLoaded", initializeCreatorPage);


(function initializeCreatorFavorite(){
    const button = document.getElementById("creatorFavoriteBtn");
    if (!button) return;
    button.addEventListener("click", async () => {
        const creator = window.MODELRADAR_CREATOR || "";
        const next = button.dataset.favorite !== "true";
        try {
            const response = await fetch(`/creator/${encodeURIComponent(creator)}/favorite`, {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({favorite: next})
            });
            const data = await response.json();
            if (!response.ok || !data.success) return;
            button.dataset.favorite = data.favorite ? "true" : "false";
            button.classList.toggle("is-favorite", !!data.favorite);
            button.textContent = data.favorite ? "♥" : "♡";
            const tip = data.favorite ? "Remove Favorite" : "Favorite Creator";
            button.title = tip; button.setAttribute("aria-label", tip); button.dataset.tooltip = tip;
        } catch (error) {
            console.error("Creator favorite update failed", error);
        }
    });
})();


(function initializeCreatorBlock(){
    const button = document.getElementById("creatorBlockBtn");
    const dialog = document.getElementById("creatorBlockDialog");
    if (!button || !dialog) return;

    const creator = window.MODELRADAR_CREATOR || "";
    const configuredSources = Array.isArray(window.MODELRADAR_CREATOR_SOURCES) ? window.MODELRADAR_CREATOR_SOURCES.filter(Boolean) : [];
    const sources = configuredSources.length
        ? configuredSources
        : Array.from(document.querySelectorAll(".creator-source-pill[data-source]"))
            .map(el => el.dataset.source)
            .filter(Boolean);
    const title = document.getElementById("creatorBlockDialogTitle");
    const text = document.getElementById("creatorBlockDialogText");
    const removeRow = document.getElementById("creatorRemoveExistingRow");
    const removeExisting = document.getElementById("creatorRemoveExisting");
    const note = document.getElementById("creatorBlockDialogNote");
    const confirmButton = document.getElementById("creatorBlockDialogConfirm");

    function setButtonState(blocked) {
        button.dataset.blocked = blocked ? "true" : "false";
        button.classList.toggle("is-blocked", blocked);
        const tip = blocked ? "Unblock Creator" : "Block Creator";
        button.title = tip; button.setAttribute("aria-label", tip); button.dataset.tooltip = tip;
    }

    button.addEventListener("click", () => {
        const blocked = button.dataset.blocked === "true";
        removeExisting.checked = false;
        removeRow.hidden = blocked;
        note.hidden = blocked;
        title.textContent = blocked ? "Unblock Creator" : "Block Creator";
        text.textContent = blocked
            ? `Allow future models from ${creator} on the source accounts shown here?`
            : `Skip future models from ${creator} on the source accounts shown here.`;
        confirmButton.textContent = blocked ? "Unblock Creator" : "Block Creator";
        confirmButton.classList.toggle("is-unblock", blocked);
        dialog.showModal();
    });

    dialog.addEventListener("close", async () => {
        if (dialog.returnValue !== "default") return;
        const blocked = button.dataset.blocked === "true";
        button.disabled = true;
        try {
            if (blocked) {
                for (const source of sources) {
                    const response = await fetch("/api/blocked-creators/unblock", {
                        method: "POST",
                        headers: {"Content-Type": "application/json"},
                        body: JSON.stringify({creator, source})
                    });
                    const data = await response.json();
                    if (!response.ok || !data.success) throw new Error(data.error || "Unable to unblock creator.");
                }
                setButtonState(false);
                return;
            }

            const response = await fetch("/api/blocked-creators", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({creator, sources, remove_existing: removeExisting.checked})
            });
            const data = await response.json();
            if (!response.ok || !data.success) throw new Error(data.error || "Unable to block creator.");
            setButtonState(true);
            if (data.protected) {
                window.alert(`${data.protected} favorite model${data.protected === 1 ? " was" : "s were"} kept. Remove the favorite first if you also want to delete those models.`);
            }
            if (removeExisting.checked && data.deleted) window.location.reload();
        } catch (error) {
            window.alert(error.message || "Unable to update blocked creator.");
        } finally {
            button.disabled = false;
        }
    });
})();
