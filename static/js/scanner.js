let scanRunning = false;
let scanTimer = null;
let scanDetailsManuallyOpen = false;

function initializeScanner(){
    const scanButton = document.getElementById("scanButton");
    const scanDetailsButton = document.getElementById("scanDetailsButton");

    if(!scanButton) return;

    const scanOverlay = document.getElementById("scanConfigOverlay");
    const closeScanConfig = () => { scanOverlay?.classList.remove("open"); scanOverlay?.setAttribute("aria-hidden","true"); };
    scanButton.addEventListener("click", () => {
        if(scanRunning) return;
        scanOverlay?.classList.add("open");
        scanOverlay?.setAttribute("aria-hidden","false");
        requestAnimationFrame(() => {
            const sourceBoxes=Array.from(document.querySelectorAll('input[name="scanSource"]'));
            const archBoxes=Array.from(document.querySelectorAll('input[name="scanArchitecture"]'));
            const sourceButton=document.getElementById("scanAllSources");
            const archButton=document.getElementById("scanAllArchitectures");
            if(sourceButton) sourceButton.textContent=(sourceBoxes.length>0&&sourceBoxes.every(x=>x.checked))?"Clear":"Select all";
            if(archButton) archButton.textContent=(archBoxes.length>0&&archBoxes.every(x=>x.checked))?"Clear":"Select all";
        });
    });
    document.getElementById("closeScanConfig")?.addEventListener("click", closeScanConfig);
    scanOverlay?.querySelectorAll(".open-settings-overlay").forEach(button => button.addEventListener("click", closeScanConfig));
    scanOverlay?.addEventListener("click", e => { if(e.target === scanOverlay) closeScanConfig(); });
    const syncBulkLabel = (buttonId, selector) => {
        const button=document.getElementById(buttonId);
        const boxes=Array.from(document.querySelectorAll(selector));
        if(!button) return;
        const all=boxes.length>0 && boxes.every(x=>x.checked);
        button.textContent=all?"Clear":"Select all";
    };
    const syncScanBulkLabels = () => {
        syncBulkLabel("scanAllSources",'input[name="scanSource"]');
        syncBulkLabel("scanAllArchitectures",'input[name="scanArchitecture"]');
    };

    document.getElementById("scanAllSources")?.addEventListener("click", () => {
        const boxes=Array.from(document.querySelectorAll('input[name="scanSource"]'));
        const all=boxes.length>0 && boxes.every(x=>x.checked);
        boxes.forEach(x=>x.checked=!all);
        syncScanBulkLabels();
    });
    document.getElementById("scanAllArchitectures")?.addEventListener("click", () => {
        const boxes=Array.from(document.querySelectorAll('input[name="scanArchitecture"]'));
        const all=boxes.length>0 && boxes.every(x=>x.checked);
        boxes.forEach(x=>x.checked=!all);
        syncScanBulkLabels();
    });

    document.querySelectorAll('input[name="scanSource"], input[name="scanArchitecture"]').forEach(box=>{
        box.addEventListener("change", syncScanBulkLabels);
    });
    syncScanBulkLabels();
    document.getElementById("runConfiguredScan")?.addEventListener("click", () => {
        const sources=Array.from(document.querySelectorAll('input[name="scanSource"]:checked')).map(x=>x.value);
        const architectures=Array.from(document.querySelectorAll('input[name="scanArchitecture"]:checked')).map(x=>x.value);
        const status=document.getElementById("scanConfigStatus");
        if(!sources.length){ if(status) status.textContent="Select at least one source."; return; }
        if(!architectures.length){ if(status) status.textContent="Select at least one model architecture."; return; }
        closeScanConfig(); startScan(sources, architectures);
    });

    scanDetailsButton?.addEventListener("click", () => {
        if(scanRunning) toggleScanDetails();
        else openScanHistory();
    });

    document.getElementById("hideScanButton")?.addEventListener("click", () => {
        if(!scanRunning) return;
        scanDetailsManuallyOpen = false;
        hideScanProgress();
    });

    document.getElementById("stopScanButton")?.addEventListener("click", async () => {
        if(!scanRunning) return;
        const button=document.getElementById("stopScanButton");
        if(button){ button.disabled=true; button.textContent="Stopping…"; }
        try{ await fetch("/scan/stop", {method:"POST"}); }catch(error){ console.error(error); }
    });

    document.getElementById("closeScanHistory")?.addEventListener("click", closeScanHistory);
    document.getElementById("scanHistoryOverlay")?.addEventListener("click", event => {
        if(event.target.id === "scanHistoryOverlay") closeScanHistory();
    });

    // Restore the current scan state when the page is refreshed or reopened.
    fetch("/scan/status", {cache:"no-store"})
        .then(response => response.json())
        .then(data => {
            renderScanStatus(data);

            if(data.status === "running"){
                scanRunning = true;
                setScanButtonRunning(true);
                showScanProgress();
                updateScanDetailsButtonLabel();
                watchScan();
            }
        })
        .catch(() => {});
}

function setScanButtonRunning(running){
    const scanButton = document.getElementById("scanButton");
    if(!scanButton) return;

    scanButton.disabled = running;
    scanButton.textContent = running ? "SCANNING…" : "SCAN";
    scanButton.classList.toggle("is-scanning", running);
    scanButton.title = running ? "An AbyssBeacon scan is running. Open Scan details to view live progress." : "Scan the currently enabled sources for models.";

    const discoveryButton = document.getElementById("discoveryScanButton");
    if(discoveryButton){
        discoveryButton.disabled = running;
        discoveryButton.title = running
            ? "An AbyssBeacon scan is already running."
            : "Discover models by tags and categories.";
    }
}

function updateScanDetailsButtonLabel(){
    const button = document.getElementById("scanDetailsButton");
    const progress = document.getElementById("scanProgress");
    if(!button) return;

    if(scanRunning){
        const visible = progress && !progress.classList.contains("hidden");
        button.textContent = visible ? "Hide Scan" : "Show Scan";
        button.title = visible
            ? "Hide live scan progress. The scan will continue in the background."
            : "Show live scan progress.";
    }else{
        button.textContent = "Scan Details";
        button.title = "View previous scan details.";
    }
}

function notifyScanVisibility(){
    updateScanDetailsButtonLabel();
    document.dispatchEvent(new CustomEvent("modelradar:scan-visibility"));
}

function showScanProgress(){
    const scanProgress = document.getElementById("scanProgress");
    if(!scanProgress) return;

    const wasHidden = scanProgress.classList.contains("hidden");
    scanProgress.classList.remove("hidden");

    if(wasHidden) notifyScanVisibility();
}

function hideScanProgress(){
    const scanProgress = document.getElementById("scanProgress");
    if(!scanProgress) return;

    const wasVisible = !scanProgress.classList.contains("hidden");
    scanProgress.classList.add("hidden");

    if(wasVisible) notifyScanVisibility();
}

function toggleScanDetails(){
    const scanProgress = document.getElementById("scanProgress");
    if(!scanProgress) return;

    const willOpen = scanProgress.classList.contains("hidden");
    scanDetailsManuallyOpen = willOpen;

    if(willOpen){
        showScanProgress();
        fetch("/scan/status", {cache:"no-store"})
            .then(response => response.json())
            .then(renderScanStatus)
            .catch(() => {});
    }else if(!scanRunning){
        hideScanProgress();
    }else{
        // During a live scan the details button is allowed to collapse the card.
        hideScanProgress();
    }
}

function startScan(configuredSources=null, configuredArchitectures=null){
    const formData = new FormData();
    const sources = configuredSources || Array.from(document.querySelectorAll('input[name="scanSource"]:checked')).map(input => input.value);
    const architectures = configuredArchitectures || Array.from(document.querySelectorAll('input[name="scanArchitecture"]:checked')).map(input => input.value);
    sources.forEach(source => formData.append("sources", source));
    architectures.forEach(architecture => formData.append("architectures", architecture));

    scanRunning = true;
    scanDetailsManuallyOpen = true;
    updateScanDetailsButtonLabel();
    const stopButton=document.getElementById("stopScanButton");
    if(stopButton){ stopButton.disabled=false; stopButton.textContent="Stop Scan"; }
    setScanButtonRunning(true);

    // Show the card before starting the request so the browser gets a chance
    // to paint the UI immediately, even on a heavy scan.
    showScanProgress();
    renderScanStatus({
        status:"running",
        source:"",
        message:"Starting scan...",
        processed:0,
        added:0,
        updated:0,
        images:0,
        videos:0,
        sources:Object.fromEntries(
            sources.map(source => [
                source,
                {
                    status:"scanning",
                    processed:0,
                    added:0,
                    updated:0,
                    images:0,
                    videos:0
                }
            ])
        )
    });

    fetch("/scan", {
        method:"POST",
        body:formData,
        cache:"no-store"
    })
    .then(response => {
        if(!response.ok) throw new Error(`Scan start failed (${response.status})`);
        return response.json();
    })
    .then(() => watchScan())
    .catch(error => {
        scanRunning = false;
        setScanButtonRunning(false);
        renderScanStatus({status:"error", message:error.message});
        showScanProgress();
    });
}

function scanSourceLabel(source){
    const labels = {
        huggingface:"Hugging Face",
        modelscope:"ModelScope",
        civitai:"CivitAI",
        civitaired:"CivitAI Red",
        tensorhub:"TensorHub Art",
        seaart:"SeaArt"
    };
    return labels[String(source || "").toLowerCase()] || String(source || "");
}

function renderScanSourceGrid(data){
    const grid = document.getElementById("scanSourceGrid");
    if(!grid) return;

    const sources = data?.sources && typeof data.sources === "object"
        ? data.sources
        : {};
    const entries = Object.entries(sources);

    if(!entries.length){
        grid.replaceChildren();
        grid.hidden = true;
        return;
    }

    grid.hidden = false;
    const fragment = document.createDocumentFragment();

    entries.forEach(([sourceName, state]) => {
        const row = document.createElement("div");
        row.className = "scan-source-row";

        const label = document.createElement("span");
        label.textContent = scanSourceLabel(sourceName);

        const result = document.createElement("span");
        result.className = "scan-source-result";

        const status = String(state?.status || "scanning").toLowerCase();
        const processed = Number(state?.processed || 0);

        const strong = document.createElement("strong");
        strong.textContent = String(processed);

        if(status === "error") strong.classList.add("scan-source-error");
        else if(status === "stopped") strong.classList.add("scan-source-stopped");

        const small = document.createElement("small");
        if(status === "complete") small.textContent = "models";
        else if(status === "error") small.textContent = "error";
        else if(status === "stopped") small.textContent = "stopped";
        else if(status === "skipped") small.textContent = "skipped";
        else small.textContent = "scanning";

        result.append(strong, small);
        row.append(label, result);
        fragment.appendChild(row);
    });

    grid.replaceChildren(fragment);
}

function renderScanStatus(data){
    renderScanSourceGrid(data);

    const message = document.getElementById("scanMessage");
    const source = document.getElementById("scanSource");
    const processed = document.getElementById("scanProcessed");
    const added = document.getElementById("scanAdded");
    const updated = document.getElementById("scanUpdated");
    const images = document.getElementById("scanImages");
    const videos = document.getElementById("scanVideos");
    const title = document.getElementById("scanTitleText");

    if(message && data.message !== undefined) message.textContent = data.message || "";
    if(source && data.source !== undefined) source.textContent = data.source || "";
    if(processed && data.processed !== undefined) processed.textContent = data.processed || 0;
    if(added && data.added !== undefined) added.textContent = data.added || 0;
    if(updated && data.updated !== undefined) updated.textContent = data.updated || 0;
    if(images && data.images !== undefined) images.textContent = data.images || 0;
    if(videos && data.videos !== undefined) videos.textContent = data.videos || 0;

    const scanButton = document.getElementById("scanButton");
    if(scanButton && data.status === "running"){
        const sourceName = data.source ? ` Current source: ${data.source}.` : "";
        scanButton.title = `Scan in progress.${sourceName} Processed: ${data.processed || 0}. New: ${data.added || 0}.`;
    }

    if(title){
        if(data.status === "complete") title.textContent = "Scan complete";
        else if(data.status === "error") title.textContent = "Scan error";
        else if(data.status === "stopped") title.textContent = "Scan stopped";
        else if(data.status === "stopping") title.textContent = "Stopping scan…";
        else if(data.status === "complete_with_errors") title.textContent = "Scan complete with errors";
        else title.textContent = "Scanning...";
    }
}

function watchScan(){
    if(scanTimer) clearInterval(scanTimer);

    const poll = () => {
        fetch("/scan/status", {cache:"no-store"})
            .then(response => response.json())
            .then(data => {
                renderScanStatus(data);

                if(data.status === "running" || data.status === "stopping") return;

                if(data.status === "complete" || data.status === "complete_with_errors" || data.status === "stopped" || data.status === "error"){
                    if(scanTimer){
                        clearInterval(scanTimer);
                        scanTimer = null;
                    }

                    scanRunning = false;
                    setScanButtonRunning(false);
                    updateScanDetailsButtonLabel();

                    const stopButton=document.getElementById("stopScanButton");
                    if(stopButton){
                        stopButton.disabled=false;
                        stopButton.textContent="Stop Scan";
                    }

                    // Keep terminal scan details visible briefly so the user can
                    // read the final summary. Successful scans still reload to
                    // refresh the feed; manually stopped scans simply dismiss
                    // the progress card without forcing a browser refresh.
                    showScanProgress();

                    if(data.status === "complete" || data.status === "complete_with_errors"){
                        setTimeout(() => window.location.reload(), 1200);
                    }else if(data.status === "stopped"){
                        // A stopped scan may still have saved models/media that
                        // completed before the stop flag was observed. Keep the
                        // stopped summary visible briefly, then refresh so feed
                        // cards and navbar counts reflect that partial work.
                        setTimeout(() => {
                            if(scanRunning) return;

                            const title=document.getElementById("scanTitleText");
                            const message=document.getElementById("scanMessage");
                            if(title) title.textContent="Refreshing AbyssBeacon…";
                            if(message) message.textContent="Applying completed scan changes…";
                        }, 2500);

                        setTimeout(() => {
                            if(!scanRunning) window.location.reload();
                        }, 3400);
                    }
                }
            })
            .catch(error => console.error("Scan status error:", error));
    };

    poll();
    scanTimer = setInterval(poll, 1000);
}


function closeScanHistory(){
    const overlay=document.getElementById("scanHistoryOverlay");
    overlay?.classList.remove("open"); overlay?.setAttribute("aria-hidden","true");
}

function openScanHistory(){
    const overlay=document.getElementById("scanHistoryOverlay");
    const content=document.getElementById("scanHistoryContent");
    overlay?.classList.add("open"); overlay?.setAttribute("aria-hidden","false");
    if(content) content.textContent="Loading…";
    fetch("/scan/history", {cache:"no-store"}).then(r=>r.json()).then(data=>{
        if(!content) return;
        if(!data.runs?.length){ content.innerHTML='<div class="scan-history-run">No scans recorded yet.</div>'; return; }
        content.innerHTML=data.runs.map(run=>{
            const sourceNames=(run.sources||[]).map(s=>s.source).join(", ") || "—";
            const duration=Number(run.duration||0).toFixed(1);
            return `<div class="scan-history-run"><div class="scan-history-top"><strong>${run.finished_ago}</strong><span>${duration}s</span></div><div class="scan-history-stats">${sourceNames}<br><strong>${run.total_added||0}</strong> new · <strong>${run.total_updated||0}</strong> updated · ${run.total_images||0} images · ${run.total_videos||0} videos</div></div>`;
        }).join("");
    }).catch(()=>{ if(content) content.textContent="Could not load scan history."; });
}
