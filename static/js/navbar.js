
window.modelRadarConfirm = window.modelRadarConfirm || function(message, options={}){
    return new Promise(resolve=>{
        const old=document.getElementById("modelRadarConfirmOverlay");
        if(old) old.remove();

        const overlay=document.createElement("div");
        overlay.id="modelRadarConfirmOverlay";
        overlay.className="modelradar-confirm-overlay";

        const box=document.createElement("div");
        box.className="modelradar-confirm-box";

        const title=document.createElement("h3");
        title.textContent=options.title || "Confirm";

        const body=document.createElement("p");
        body.textContent=message;

        const actions=document.createElement("div");
        actions.className="modelradar-confirm-actions";

        const cancel=document.createElement("button");
        cancel.type="button";
        cancel.className="modelradar-confirm-cancel";
        cancel.textContent=options.cancelText || "Cancel";

        const ok=document.createElement("button");
        ok.type="button";
        ok.className="modelradar-confirm-ok";
        ok.textContent=options.okText || "Forget";

        const finish=value=>{
            overlay.classList.remove("open");
            setTimeout(()=>overlay.remove(),130);
            resolve(value);
        };

        cancel.addEventListener("click",()=>finish(false));
        ok.addEventListener("click",()=>finish(true));
        overlay.addEventListener("click",event=>{
            if(event.target===overlay) finish(false);
        });
        document.addEventListener("keydown",function esc(event){
            if(event.key==="Escape"){
                document.removeEventListener("keydown",esc);
                finish(false);
            }
        },{once:true});

        actions.append(cancel,ok);
        box.append(title,body,actions);
        overlay.append(box);
        document.body.append(overlay);
        requestAnimationFrame(()=>overlay.classList.add("open"));
        ok.focus();
    });
};

function initializeNavbar(){

    const optionsButton = document.getElementById("optionsButton");
    const optionsPanel = document.getElementById("optionsPanel");
    const optionsMenu = optionsPanel?.querySelector(".options-menu");
    const optionPages = optionsPanel?.querySelectorAll(".options-page") || [];
    const optionsWrapper = document.querySelector(".options-wrapper");

    const settingsOverlay = document.getElementById("settingsOverlay");
    const settingsFrame = document.getElementById("settingsFrame");
    const settingsTitle = document.getElementById("settingsOverlayTitle");
    const closeSettings = document.getElementById("closeSettingsOverlay");

    const scanProgress = document.getElementById("scanProgress");
    const navbar = document.querySelector(".navbar");
    const settingsModal = settingsOverlay?.querySelector(".settings-modal");

    const favoriteCreatorsOverlay = document.getElementById("favoriteCreatorsOverlay");
    const favoriteCreatorsContent = document.getElementById("favoriteCreatorsContent");
    const favoriteCreatorsSearch = document.getElementById("favoriteCreatorsSearch");
    let favoriteCreatorsCache = [];

    function renderFavoriteCreators(){
        if(!favoriteCreatorsContent) return;
        const query=(favoriteCreatorsSearch?.value || "").trim().toLowerCase();
        const rows=favoriteCreatorsCache.filter(item => !query || item.name.toLowerCase().includes(query));
        if(!rows.length){
            favoriteCreatorsContent.innerHTML='<div class="favorite-creators-empty">No favorite creators match this search.</div>';
            return;
        }
        favoriteCreatorsContent.replaceChildren(...rows.map(item => {
            const row=document.createElement("div"); row.className="favorite-creator-row";
            const info=document.createElement("div");
            const name=document.createElement("div"); name.className="favorite-creator-name"; name.textContent=item.name;
            const meta=document.createElement("div"); meta.className="favorite-creator-meta";
            const sources=(item.sources || []).join(" · ") || "No source recorded";
            meta.textContent=`${item.model_count} model${item.model_count===1?"":"s"} · ${sources} · Last scan ${item.last_scan}`;
            info.append(name,meta);
            const view=document.createElement("a"); view.className="favorite-creator-view"; view.href=`/creator/${encodeURIComponent(item.name)}`; view.textContent="View Creator";
            const heart=document.createElement("button"); heart.type="button"; heart.className="favorite-creator-unfavorite"; heart.textContent="♥"; heart.title="Remove from favorite creators";
            heart.addEventListener("click", async event => {
                event.stopPropagation();
                const response=await fetch(`/creator/${encodeURIComponent(item.name)}/favorite`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({favorite:false})});
                if(!response.ok) return;
                favoriteCreatorsCache=favoriteCreatorsCache.filter(entry=>entry.name.toLowerCase()!==item.name.toLowerCase());
                if(window.favoriteCreatorNames) window.favoriteCreatorNames.delete(item.name.toLowerCase());
                renderFavoriteCreators();
                if(typeof filterCards === "function") filterCards();
            });
            row.append(info,view,heart); return row;
        }));
    }

    async function openFavoriteCreators(){
        if(!favoriteCreatorsOverlay || !favoriteCreatorsContent) return;
        optionsPanel?.classList.remove("open");
        favoriteCreatorsOverlay.classList.add("open"); favoriteCreatorsOverlay.setAttribute("aria-hidden","false");
        favoriteCreatorsContent.textContent="Loading…";
        try{
            const response=await fetch("/api/favorite-creators");
            const data=await response.json();
            favoriteCreatorsCache=Array.isArray(data.creators)?data.creators:[];
            renderFavoriteCreators();
        }catch(_){ favoriteCreatorsContent.innerHTML='<div class="favorite-creators-empty">Unable to load favorite creators.</div>'; }
    }

    function closeFavoriteCreators(){ favoriteCreatorsOverlay?.classList.remove("open"); favoriteCreatorsOverlay?.setAttribute("aria-hidden","true"); }

    function scanIsVisible(){
        return scanProgress && !scanProgress.classList.contains("hidden");
    }

    function clearScanDockClasses(){
        scanProgress?.classList.remove("options-docked");
        optionsWrapper?.style.removeProperty("--options-panel-height");
        settingsOverlay?.classList.remove("with-scan-status");
    }

    function dockScanProgressBelowOptions(){
        if(!scanProgress || !optionsPanel || !optionsWrapper) return;
        if(!optionsPanel.classList.contains("open")) return;
        if(settingsOverlay?.classList.contains("open")) return;

        // Keep the scanner status directly below the currently open gear panel.
        // The panel can change height as sub-pages are opened, so measure it each time.
        const panelHeight = optionsPanel.getBoundingClientRect().height;
        optionsWrapper.style.setProperty("--options-panel-height", `${panelHeight}px`);

        if(scanProgress.parentElement !== optionsWrapper){
            optionsWrapper.appendChild(scanProgress);
        }

        scanProgress.classList.add("options-docked");
        settingsOverlay?.classList.remove("with-scan-status");
    }

    function dockScanProgressInSettings(){
        if(!scanProgress || !settingsOverlay || !settingsModal) return;

        clearScanDockClasses();

        // Keep the live scanner card under the settings dialog rather than over it.
        // Re-parenting preserves the live IDs used by scanner.js.
        if(scanProgress.parentElement !== settingsOverlay){
            settingsOverlay.appendChild(scanProgress);
        }

        settingsOverlay.classList.add("with-scan-status");
    }

    function restoreScanProgressToNavbar(){
        if(!scanProgress || !navbar) return;

        clearScanDockClasses();

        if(scanProgress.parentElement !== navbar){
            navbar.appendChild(scanProgress);
        }
    }

    function refreshScanDock(){
        if(settingsOverlay?.classList.contains("open")){
            dockScanProgressInSettings();
        }else if(optionsPanel?.classList.contains("open")){
            requestAnimationFrame(dockScanProgressBelowOptions);
        }else{
            restoreScanProgressToNavbar();
        }
    }

    // scanner.js emits this event when the scan card is shown/hidden.
    // Do not watch scanProgress.class with a MutationObserver here: docking itself
    // changes classes, which can recursively retrigger the observer and lock the UI.
    document.addEventListener("modelradar:scan-visibility", () => {
        if(scanIsVisible()) refreshScanDock();
        else restoreScanProgressToNavbar();
    });

    function showOptionsPage(pageName = "filters"){
        if(!optionsMenu) return;
        optionsMenu.style.display = "flex";
        optionPages.forEach(page => page.style.display = page.dataset.optionsPage === pageName ? "block" : "none");
        optionsPanel?.querySelectorAll(".options-item[data-page]").forEach(button => button.classList.toggle("active", button.dataset.page === pageName));
        if(optionsPanel) optionsPanel.scrollTop = 0;
        requestAnimationFrame(dockScanProgressBelowOptions);
    }

    function openSettingsOverlay(url, title){
        if(!settingsOverlay || !settingsFrame) return;
        settingsFrame.src = url;
        if(settingsTitle) settingsTitle.textContent = title || "Settings";
        settingsOverlay.classList.add("open");
        settingsOverlay.setAttribute("aria-hidden", "false");
        optionsPanel?.classList.remove("open");

        if(scanIsVisible()) dockScanProgressInSettings();
        else restoreScanProgressToNavbar();
    }

    function reloadAndRestoreSettings(url, title){
        try{
            sessionStorage.setItem("modelradar:restore-settings", JSON.stringify({
                url: url || settingsFrame?.getAttribute("src") || "/settings",
                title: title || settingsTitle?.textContent || "Settings"
            }));
        }catch(error){}
        window.location.reload();
    }

    function closeSettingsOverlay(){
        if(!settingsOverlay || !settingsFrame) return;
        settingsOverlay.classList.remove("open");
        settingsOverlay.setAttribute("aria-hidden", "true");
        settingsFrame.src = "about:blank";

        restoreScanProgressToNavbar();
    }

    optionsPanel?.querySelectorAll(".options-item[data-page]").forEach(button => {
        button.addEventListener("click", event => {
            event.stopPropagation();
            showOptionsPage(button.dataset.page);
        });
    });

    document.querySelectorAll(".open-settings-overlay").forEach(button => {
        button.addEventListener("click", event => {
            event.stopPropagation();
            openSettingsOverlay(button.dataset.settingsUrl, button.dataset.settingsTitle);
        });
    });

    closeSettings?.addEventListener("click", closeSettingsOverlay);

    document.getElementById("closeOptionsReturn")?.addEventListener("click", event => {
        event.stopPropagation();
        optionsPanel?.classList.remove("open");
        restoreScanProgressToNavbar();
    });

    // Settings pages are loaded inside a same-origin iframe.  Let an embedded
    // settings page ask the parent to close the overlay instead of navigating
    // the iframe back to /, which would create AbyssBeacon inside AbyssBeacon.
    window.addEventListener("message", event => {
        if(event.origin !== window.location.origin) return;
        if(event.source !== settingsFrame?.contentWindow) return;
        if(event.data?.type === "modelradar:close-settings"){
            closeSettingsOverlay();
            return;
        }
        if(event.data?.type === "modelradar:open-scan-settings-page"){
            const url=String(event.data.url || "");
            const title=String(event.data.title || "Settings");
            if(url === "/settings" || url === "/settings/sources" || url === "/settings/architectures"){
                openSettingsOverlay(url, title);
            }
            return;
        }
        if(event.data?.type === "modelradar:show-scan-window"){
            closeSettingsOverlay();
            const scanButton=document.getElementById("scanButton");
            if(scanButton && !scanButton.disabled){
                requestAnimationFrame(() => scanButton.click());
            }
            return;
        }
        if(event.data?.type === "modelradar:open-retention-settings"){
            closeSettingsOverlay();
            showOptionsPage("library");
            optionsPanel?.classList.add("open");
            const retention = document.getElementById("automaticRetentionSettings");
            if(retention){
                retention.open = true;
                requestAnimationFrame(() => {
                    retention.scrollIntoView({behavior:"smooth", block:"center"});
                    retention.classList.remove("retention-link-highlight");
                    void retention.offsetWidth;
                    retention.classList.add("retention-link-highlight");
                    window.setTimeout(() => retention.classList.remove("retention-link-highlight"), 1800);
                });
            }
            requestAnimationFrame(dockScanProgressBelowOptions);
            return;
        }
        if(event.data?.type === "modelradar:scan-definitions-saved"){
            reloadAndRestoreSettings(event.data.url, event.data.title);
        }
    });

    try{
        const restoreRaw=sessionStorage.getItem("modelradar:restore-settings");
        if(restoreRaw){
            sessionStorage.removeItem("modelradar:restore-settings");
            const restore=JSON.parse(restoreRaw);
            requestAnimationFrame(() => openSettingsOverlay(restore.url || "/settings", restore.title || "Settings"));
        }
    }catch(error){
        try{ sessionStorage.removeItem("modelradar:restore-settings"); }catch(ignore){}
    }
    settingsOverlay?.addEventListener("click", event => {
        if(event.target === settingsOverlay) closeSettingsOverlay();
    });
    document.addEventListener("keydown", event => {
        if(event.key === "Escape" && settingsOverlay?.classList.contains("open")){
            closeSettingsOverlay();
        }
    });

    if(optionsButton && optionsPanel){
        optionsButton.addEventListener("click", event => {
            event.stopPropagation();
            const open = optionsPanel.classList.contains("open");
            if(open){
                optionsPanel.classList.remove("open");
                restoreScanProgressToNavbar();
                return;
            }
            showOptionsPage("filters");
            optionsPanel.classList.add("open");
            requestAnimationFrame(dockScanProgressBelowOptions);
        });

        optionsPanel.addEventListener("click", event => event.stopPropagation());
    }

    document.addEventListener("click", () => {
        if(optionsPanel?.classList.contains("open")){
            optionsPanel.classList.remove("open");
            restoreScanProgressToNavbar();
        }
    });

    window.addEventListener("resize", () => {
        if(optionsPanel?.classList.contains("open")){
            requestAnimationFrame(dockScanProgressBelowOptions);
        }
    });

    document.getElementById("browseFavoriteCreators")?.addEventListener("click", event => { event.stopPropagation(); openFavoriteCreators(); });
    document.getElementById("closeFavoriteCreators")?.addEventListener("click", closeFavoriteCreators);
    favoriteCreatorsOverlay?.addEventListener("click", event => { if(event.target===favoriteCreatorsOverlay) closeFavoriteCreators(); });
    favoriteCreatorsSearch?.addEventListener("input", renderFavoriteCreators);
    const applySort = document.getElementById("applySort");
    applySort?.addEventListener("click", event => {
        event.stopPropagation();
        const selected = document.querySelector('input[name="sortOption"]:checked');
        if(!selected) return;
        const url = new URL(window.location.href);
        url.searchParams.set("sort", selected.value);
        window.location.href = url.toString();
    });


}

function updateSourceButton(){}

function updateSourcePills(){
    const inputs = Array.from(document.querySelectorAll('input[name="sources"]'));
    inputs.forEach(input => {
        input.closest(".source-pill")?.classList.toggle("active", input.checked);
    });

    const summary = document.getElementById("scannerSourceSummary");
    if(summary){
        const labels = inputs
            .filter(input => input.checked)
            .map(input => input.closest("label")?.querySelector("span")?.textContent?.trim() || input.value);
        summary.textContent = labels.length ? labels.join(" · ") : "No sources selected";
    }
}


// Scanner diagnostics preference
document.getElementById("verboseScanLogging")?.addEventListener("change", async event => {
    try {
        await fetch("/save_preferences", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({verbose_scan_logging:event.target.checked})});
    } catch(error) { console.error("Could not save verbose scan logging preference", error); }
});

// Privacy-redacted support diagnostics. The report itself is generated server-side.
async function fetchDiagnosticReport(){
    const response=await fetch("/api/diagnostic-report", {cache:"no-store"});
    if(!response.ok) throw new Error(`Diagnostic report failed (${response.status})`);
    return await response.text();
}

async function copyDiagnosticText(text){
    if(navigator.clipboard?.writeText){
        await navigator.clipboard.writeText(text);
        return;
    }
    const textarea=document.createElement("textarea");
    textarea.value=text;
    textarea.setAttribute("readonly", "");
    textarea.style.position="fixed";
    textarea.style.opacity="0";
    document.body.appendChild(textarea);
    textarea.select();
    const copied=document.execCommand("copy");
    textarea.remove();
    if(!copied) throw new Error("Clipboard copy was blocked by the browser.");
}

const diagnosticStatus=document.getElementById("diagnosticStatus");
const copyDiagnosticButton=document.getElementById("copyDiagnosticReport");
const exportDiagnosticButton=document.getElementById("exportDiagnosticReport");

copyDiagnosticButton?.addEventListener("click", async event => {
    event.stopPropagation();
    if(diagnosticStatus) diagnosticStatus.textContent="Building report...";
    copyDiagnosticButton.disabled=true;
    try {
        const report=await fetchDiagnosticReport();
        await copyDiagnosticText(report);
        if(diagnosticStatus) diagnosticStatus.textContent="Copied. Ready to paste into an issue.";
    } catch(error) {
        console.error(error);
        if(diagnosticStatus) diagnosticStatus.textContent=error?.message || "Could not copy diagnostic report.";
    } finally {
        copyDiagnosticButton.disabled=false;
    }
});

exportDiagnosticButton?.addEventListener("click", async event => {
    event.stopPropagation();
    if(diagnosticStatus) diagnosticStatus.textContent="Building report...";
    exportDiagnosticButton.disabled=true;
    try {
        const report=await fetchDiagnosticReport();
        const blob=new Blob([report], {type:"text/plain;charset=utf-8"});
        const url=URL.createObjectURL(blob);
        const stamp=new Date().toISOString().replace(/[:]/g, "-").replace(/\..+$/, "");
        const link=document.createElement("a");
        link.href=url;
        link.download=`AbyssBeacon_Diagnostic_${stamp}.txt`;
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(()=>URL.revokeObjectURL(url), 1000);
        if(diagnosticStatus) diagnosticStatus.textContent="Diagnostic report exported.";
    } catch(error) {
        console.error(error);
        if(diagnosticStatus) diagnosticStatus.textContent=error?.message || "Could not export diagnostic report.";
    } finally {
        exportDiagnosticButton.disabled=false;
    }
});

// Global media sanity limit used by scanner.py. 0 means unlimited.
document.getElementById("mediaPerModelLimit")?.addEventListener("change", async event => {
    const input=event.target;
    const parsed=Number.parseInt(input.value || "0", 10);
    const value=Number.isFinite(parsed) ? Math.max(0, parsed) : 100;
    input.value=String(value);
    try {
        await fetch("/save_preferences", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({media_per_model_limit:value})});
    } catch(error) { console.error("Could not save media-per-model limit", error); }
});



document.addEventListener("DOMContentLoaded",()=>{
    const autoEnabled=document.getElementById("autoCleanupEnabled");
    const autoSettings=document.getElementById("autoCleanupSettings");
    const autoDays=document.getElementById("autoCleanupDays");
    const creatorDays=document.getElementById("creatorCleanupDays");
    let saveTimer=null;
    const saveAuto=()=>{
        clearTimeout(saveTimer);
        saveTimer=setTimeout(()=>fetch("/save_preferences",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({auto_cleanup_enabled:autoEnabled?.checked===true,auto_cleanup_days:Math.max(0,Number.parseInt(autoDays?.value||"7",10)||0),creator_cleanup_days:Math.max(0,Number.parseInt(creatorDays?.value||"30",10)||0)})}).catch(()=>{}),150);
    };
    const syncAutoCleanupVisibility=()=>{
        if(autoSettings) autoSettings.hidden=autoEnabled?.checked!==true;
    };
    syncAutoCleanupVisibility();
    autoEnabled?.addEventListener("change",()=>{syncAutoCleanupVisibility();saveAuto();});
    autoDays?.addEventListener("change",saveAuto);
    creatorDays?.addEventListener("change",saveAuto);
    const repairMissingToggle=document.getElementById("downloadMissingCardPreviews");
    const repairButton=document.getElementById("repairPreviewCache");
    const previewStatus=document.getElementById("previewCacheStatus");

    const runPreviewRepair=async(button=null)=>{
        const originalText=button?.textContent||"";
        if(button){
            button.disabled=true;
            button.textContent="Downloading missing previews…";
        }
        if(previewStatus) previewStatus.textContent="Repair is running. Large libraries can take several minutes…";
        try{
            const r=await fetch("/api/library/preview-cache/repair",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});
            const d=await r.json();
            if(!r.ok||!d.success) throw new Error(d.error||"Preview repair failed");
            if(previewStatus){
                previewStatus.textContent=d.missing
                    ? `Restored ${d.repaired||0} of ${d.missing||0} missing preview${d.missing===1?"":"s"}${d.failed?` · ${d.failed} unavailable`:""}. Refresh the feed to display restored cards.`
                    : "No missing card previews found.";
            }
        }catch(e){
            if(previewStatus) previewStatus.textContent=e.message;
        }finally{
            if(button){
                button.disabled=false;
                button.textContent=originalText;
            }
        }
    };

    repairMissingToggle?.addEventListener("change",async event=>{
        const enabled=event.target.checked===true;
        try{
            await fetch("/save_preferences",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({download_missing_card_previews:enabled})});
            if(enabled) await runPreviewRepair();
        }catch(error){
            if(previewStatus) previewStatus.textContent="Could not save preview repair setting.";
        }
    });

    repairButton?.addEventListener("click",async event=>runPreviewRepair(event.currentTarget));

    document.getElementById("cleanPreviewCache")?.addEventListener("click",async event=>{
        const button=event.currentTarget, status=document.getElementById("previewCacheStatus"); button.disabled=true;
        if(status) status.textContent="Checking preview cache…";
        try{const r=await fetch("/api/library/preview-cache/clean",{method:"POST"}); const d=await r.json(); if(!r.ok||!d.success) throw new Error(d.error||"Cache cleanup failed"); const mb=(Number(d.bytes_freed||0)/1048576).toFixed(1); if(status) status.textContent=`Removed ${d.removed||0} orphaned preview${d.removed===1?"":"s"} · freed ${mb} MB.`;}catch(e){if(status) status.textContent=e.message;}finally{button.disabled=false;}
    });
});

// Live source/card color customization.
document.addEventListener("DOMContentLoaded", () => {
    const colorInputs = Array.from(document.querySelectorAll(".source-card-color"));
    if (!colorInputs.length) return;
    let timer = null;
    const currentColors = () => Object.fromEntries(colorInputs.map(input => [input.dataset.source, input.value]));
    const applyColor = (source, color) => {
        document.querySelectorAll(`.model-card[data-source="${CSS.escape(source)}"]`).forEach(card => card.style.setProperty("--source-color", color));
        document.querySelectorAll(`.model-card[data-sources~="${CSS.escape(source)}"]`).forEach(card => {
            if (card.dataset.source === source) card.style.setProperty("--source-color", color);
        });
    };
    const save = () => {
        clearTimeout(timer);
        timer = setTimeout(() => fetch("/save_preferences", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({source_card_colors:currentColors()})}).catch(()=>{}), 120);
    };
    colorInputs.forEach(input => input.addEventListener("input", () => { applyColor(input.dataset.source, input.value); save(); }));
    document.querySelectorAll(".card-color-reset").forEach(button => button.addEventListener("click", () => {
        const row = button.closest(".card-color-row"), input = row?.querySelector(".source-card-color"); if (!row || !input) return;
        input.value = row.dataset.defaultColor; applyColor(input.dataset.source, input.value); save();
    }));
    document.getElementById("resetAllCardColors")?.addEventListener("click", () => {
        colorInputs.forEach(input => { const row=input.closest(".card-color-row"); if(row){input.value=row.dataset.defaultColor; applyColor(input.dataset.source,input.value);} }); save();
    });
});


// Source/architecture library cleanup. This never touches downloaded model files.
document.addEventListener("DOMContentLoaded",()=>{
 const overlay=document.getElementById("bulkLibraryCleanupOverlay"), summary=document.getElementById("bulkCleanupSummary"), confirm=document.getElementById("confirmBulkLibraryCleanup"); let preview=null;
 const vals=name=>Array.from(document.querySelectorAll(`input[name="${name}"]:checked`)).map(x=>x.value);
 const close=()=>{overlay?.classList.remove("open");overlay?.setAttribute("aria-hidden","true");preview=null;if(confirm)confirm.disabled=true;};
 document.getElementById("openBulkLibraryCleanup")?.addEventListener("click",()=>{overlay?.classList.add("open");overlay?.setAttribute("aria-hidden","false");});
 document.getElementById("closeBulkLibraryCleanup")?.addEventListener("click",close); document.getElementById("cancelBulkLibraryCleanup")?.addEventListener("click",close); overlay?.addEventListener("click",e=>{if(e.target===overlay)close();});
 const setAllSources=checked=>{document.querySelectorAll('input[name="bulkCleanupSource"]').forEach(input=>{input.checked=checked;});preview=null;if(confirm)confirm.disabled=true;if(summary)summary.textContent=checked?"All sources selected. Preview to review deletion.":"Select at least one source, then preview.";};
 document.getElementById("bulkSelectAllSources")?.addEventListener("click",()=>setAllSources(true));
 document.getElementById("bulkClearAllSources")?.addEventListener("click",()=>setAllSources(false));
 const setAllArchitectures=checked=>{document.querySelectorAll('input[name="bulkCleanupArchitecture"]').forEach(input=>{input.checked=checked;});preview=null;if(confirm)confirm.disabled=true;if(summary)summary.textContent=checked?"All architectures selected. Preview to review deletion.":"Select at least one architecture, then preview.";};
 document.getElementById("bulkSelectAllArchitectures")?.addEventListener("click",()=>setAllArchitectures(true));
 document.getElementById("bulkClearAllArchitectures")?.addEventListener("click",()=>setAllArchitectures(false));
 document.getElementById("previewBulkLibraryCleanup")?.addEventListener("click",async()=>{
   const sources=vals("bulkCleanupSource"), architectures=vals("bulkCleanupArchitecture"), mode=document.getElementById("bulkCleanupMode")?.value||"keep_selected";
   if(!sources.length||!architectures.length){if(summary)summary.textContent="Select at least one source and one architecture.";return;}
   const q=new URLSearchParams({mode});sources.forEach(x=>q.append("source",x));architectures.forEach(x=>q.append("architecture",x));
   try{const r=await fetch(`/api/library/bulk-preview?${q}`),d=await r.json();if(!r.ok||!d.success)throw new Error(d.error||"Preview failed");preview=d;const include=document.getElementById("bulkIncludeProtected")?.checked===true;const count=include?d.matched:d.deletable;if(summary)summary.innerHTML=`<div class="cleanup-main-count"><strong>${count}</strong> model${count===1?"":"s"} will be deleted</div><div><strong>${d.protected}</strong> protected model${d.protected===1?" is":"s are"} ${include?"included":"being kept"}.</div>`;if(confirm){confirm.disabled=count===0;confirm.textContent=`Delete ${count} Model${count===1?"":"s"}`;}}catch(e){if(summary)summary.textContent=e.message;}
 });
 document.getElementById("bulkIncludeProtected")?.addEventListener("change",()=>document.getElementById("previewBulkLibraryCleanup")?.click());
 confirm?.addEventListener("click",async()=>{if(!preview)return;confirm.disabled=true;confirm.textContent="Deleting…";try{const r=await fetch("/api/library/bulk-delete",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({sources:preview.sources,architectures:preview.architectures,mode:preview.mode,include_protected:document.getElementById("bulkIncludeProtected")?.checked===true})}),d=await r.json();if(!r.ok||!d.success)throw new Error(d.error||"Delete failed");window.location.reload();}catch(e){if(summary)summary.textContent=e.message;confirm.disabled=false;}});
});


// Download tracking/privacy + history cleanup.
document.addEventListener("DOMContentLoaded",()=>{
    const track=document.getElementById("trackDownloads"), show=document.getElementById("showDownloadStatusCards");
    const save=()=>fetch("/save_preferences",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({track_downloads:track?.checked===true,show_download_status_cards:show?.checked===true})}).catch(()=>{});
    track?.addEventListener("change",save); show?.addEventListener("change",save);
    const mode=document.getElementById("downloadHistoryCleanupMode"), daysRow=document.getElementById("downloadHistoryDaysRow"), days=document.getElementById("downloadHistoryDays"), status=document.getElementById("downloadHistoryStatus"), button=document.getElementById("clearDownloadHistory");
    const sync=()=>daysRow?.classList.toggle("hidden",mode?.value!=="older_than"); mode?.addEventListener("change",sync); sync();
    button?.addEventListener("click",async()=>{
        const m=mode?.value||"all", d=Math.max(1,Number.parseInt(days?.value||"30",10)||30); button.disabled=true;
        try{const q=new URLSearchParams({mode:m,days:String(d)}),r=await fetch(`/api/download-history/preview?${q}`),data=await r.json();if(!r.ok||!data.success)throw new Error(data.error||"Preview failed");
            if(!data.count){if(status)status.textContent="No matching download-history entries.";return;}
            const label=m==="all"?"all download history":m==="recent_hour"?"download history from the past hour":m==="recent_1"?"download history from the past 24 hours":m==="recent_7"?"download history from the past 7 days":`download history older than ${d} days`;
            if(!(await window.modelRadarConfirm(
                `Remove ${data.count} entr${data.count===1?"y":"ies"} from ${label}? Downloaded model files will not be deleted.`,
                {title:"Clear download history?", okText:"Clear history"}
            )))return;
            const del=await fetch("/api/download-history/clear",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({mode:m,days:d})}),out=await del.json();if(!del.ok||!out.success)throw new Error(out.error||"Cleanup failed");if(status)status.textContent=`Deleted ${out.deleted||0} download-history entr${out.deleted===1?"y":"ies"}.`;setTimeout(()=>window.location.reload(),350);
        }catch(e){if(status)status.textContent=e.message||"History cleanup failed.";}finally{button.disabled=false;}
    });
});


// Local installer preferences.
document.addEventListener("DOMContentLoaded",()=>{
    const behavior=document.getElementById("downloadBehavior");
    const root=document.getElementById("localModelsRoot");
    const layout=document.getElementById("installLayout");
    const friendly=document.getElementById("friendlyFilenames");
    const existing=document.getElementById("existingFileBehavior");
    const info=document.getElementById("saveModelInfo");
    const preview=document.getElementById("saveModelPreview");
    const test=document.getElementById("testLocalModelsRoot");
    const status=document.getElementById("localInstallerStatus");
    let timer=null;
    const payload=()=>({
        download_behavior:behavior?.value||"browser",
        local_comfy_root:(root?.value||"").trim(),
        install_layout:layout?.value||"simple",
        friendly_filenames:friendly?.value||"off",
        existing_file_behavior:existing?.value||"keep_both",
        save_model_info:info?.checked!==false,
        save_model_preview:preview?.checked!==false,
        queued_download_behavior:document.getElementById("queuedDownloadBehavior")?.value || "ask",
    });
    const save=()=>{
        const values=payload();
        window.userPreferences=Object.assign({},window.userPreferences||{},values);
        fetch("/save_preferences",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(values)}).catch(()=>{});
    };
    const delayed=()=>{clearTimeout(timer);timer=setTimeout(save,180);};
    [behavior,layout,friendly,existing,info,preview,document.getElementById("queuedDownloadBehavior")].forEach(el=>el?.addEventListener("change",save));
    root?.addEventListener("input",delayed);
    root?.addEventListener("change",save);
    test?.addEventListener("click",async()=>{
        if(status) status.textContent="Testing…";
        try{
            const response=await fetch("/api/install/test-path",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({path:(root?.value||"").trim()})});
            const data=await response.json();
            if(status){status.textContent=data.message||data.error||"Unable to test folder.";status.style.color=response.ok?"#78e8ad":"#ff8f9c";}
            if(response.ok){
                if(data.normalized_path && root) root.value=data.normalized_path;
                save();
            }
        }catch(error){if(status){status.textContent=error.message||"Unable to test folder.";status.style.color="#ff8f9c";}}
    });
});


// Recent download history in the main navbar.
document.addEventListener("DOMContentLoaded",()=>{
    const button=document.getElementById("downloadHistoryButton");
    const overlay=document.getElementById("downloadHistoryOverlay");
    const close=document.getElementById("closeDownloadHistory");
    const list=document.getElementById("downloadHistoryList");
    const activeTab=document.getElementById("downloadManagerActiveTab");
    const historyTab=document.getElementById("downloadManagerHistoryTab");
    const queueTab=document.getElementById("downloadManagerQueueTab");
    const watchTab=document.getElementById("downloadManagerWatchTab");
    const activePane=document.getElementById("downloadActivePane");
    const historyPane=document.getElementById("downloadHistoryPane");
    const queuePane=document.getElementById("downloadQueuePane");
    const watchPane=document.getElementById("downloadWatchPane");
    const queueList=document.getElementById("downloadQueueList");
    const queueCount=document.getElementById("downloadQueueCount");
    const watchList=document.getElementById("downloadWatchList");
    const watchCount=document.getElementById("downloadWatchCount");
    if(!button||!overlay||!list)return;

    const setManagerTab=name=>{
        const active=name==="active";
        const queue=name==="queue";
        const watch=name==="watch";
        const history=!active&&!queue&&!watch;
        activeTab?.classList.toggle("active",active);
        historyTab?.classList.toggle("active",history);
        queueTab?.classList.toggle("active",queue);
        watchTab?.classList.toggle("active",watch);
        activePane?.classList.toggle("active",active);
        historyPane?.classList.toggle("active",history);
        queuePane?.classList.toggle("active",queue);
        watchPane?.classList.toggle("active",watch);
        if(queue) loadQueue();
        if(watch) loadWatchlist();
        if(active) window.modelRadarPollActiveDownloads?.();
    };

    const sourceLabel=value=>({
        huggingface:"Hugging Face",
        modelscope:"ModelScope",
        civitai:"CivitAI",
        civitaired:"CivitAI Red",
        tensorhub:"TensorHub Art",
        seaart:"SeaArt"
    }[String(value||"").toLowerCase()]||value||"Source");

    const age=value=>{
        const then=new Date(value), now=new Date();
        if(Number.isNaN(then.getTime()))return "";
        const sec=Math.max(0,Math.floor((now-then)/1000));
        if(sec<60)return "just now";
        if(sec<3600)return `${Math.floor(sec/60)} min ago`;
        if(sec<86400)return `${Math.floor(sec/3600)} hr ago`;
        if(sec<604800)return `${Math.floor(sec/86400)} day${Math.floor(sec/86400)===1?"":"s"} ago`;
        return then.toLocaleString();
    };

    const render=items=>{
        if(!items?.length){
            list.innerHTML='<div class="download-history-empty">No download history yet.</div>';
            return;
        }
        list.innerHTML="";
        items.forEach(item=>{
            const row=document.createElement("div");
            row.className="download-history-row";
            const main=document.createElement("div");
            main.className="download-history-main";
            const name=document.createElement("button");
            name.type="button";
            name.className="download-history-model-link";
            name.textContent=item.model_name||item.filename||item.model_key||"Downloaded model";
            name.style.setProperty("--history-source-color", item.source_color||"#00eaff");
            name.title="Open this model in AbyssBeacon";
            name.addEventListener("click",event=>{
                event.stopPropagation();
                overlay.classList.remove("open");
                overlay.setAttribute("aria-hidden","true");
                if(typeof window.modelRadarOpenModel==="function" && window.modelRadarOpenModel(item.model_id)) return;
                window.modelRadarAlert?.("This model is no longer present in the current AbyssBeacon feed.",{title:"Model unavailable"});
            });

            const meta=document.createElement("span");
            meta.textContent=`${item.filename||"model file"} • ${age(item.downloaded_at)}`;
            main.append(name,meta);

            if(item.local_path){
                const saved=document.createElement("div");
                saved.className="download-history-saved-row";

                const path=document.createElement("span");
                path.className="download-history-path";
                path.textContent=item.display_path||item.local_path;
                path.title=item.local_path;

                const open=document.createElement("button");
                open.type="button";
                open.className="download-history-open";
                open.textContent="Open";
                open.title=`Open folder: ${item.local_path}`;
                open.addEventListener("click",async event=>{
                    event.stopPropagation();
                    open.disabled=true;
                    try{
                        const response=await fetch(`/api/download-history/${item.id}/open-folder`,{method:"POST"});
                        const data=await response.json().catch(()=>({}));
                        if(!response.ok||!data.success)throw new Error(data.error||"Could not open folder.");
                    }catch(error){
                        window.modelRadarAlert?.(error.message||"Could not open folder",{title:"Open folder"});
                    }finally{open.disabled=false;}
                });
                saved.append(open,path);
                main.append(saved);
            }
            row.append(main);
            if(item.model_id){
                const forget=document.createElement("button");
                forget.type="button";
                forget.className="download-history-forget";
                forget.textContent="Forget";
                forget.title="Remove this model from AbyssBeacon download/update tracking";
                forget.addEventListener("click",async event=>{
                    event.stopPropagation();
                    if(!(await window.modelRadarConfirm(
                        `Forget download history for "${name.textContent}"? The downloaded model file will stay exactly where it is.`,
                        {title:"Forget download history?", okText:"Forget history"}
                    )))return;
                    forget.disabled=true;
                    try{
                        const response=await fetch(`/api/download-history/model/${item.model_id}`,{method:"DELETE"});
                        const data=await response.json();
                        if(!response.ok||!data.success)throw new Error(data.error||"Unable to clear history.");
                        row.remove();
                        if(!list.querySelector(".download-history-row"))render([]);
                    }catch(error){
                        window.alert(error.message||"Unable to clear history.");
                        forget.disabled=false;
                    }
                });
                row.append(forget);
            }
            list.append(row);
        });
    };

    const load=async()=>{
        list.innerHTML='<div class="download-history-empty">Loading…</div>';
        try{
            const response=await fetch("/api/download-history/recent?limit=40");
            const data=await response.json();
            if(!response.ok||!data.success)throw new Error(data.error||"Unable to load history.");
            render(data.items||[]);
        }catch(error){
            list.innerHTML=`<div class="download-history-empty">${error.message||"Unable to load download history."}</div>`;
        }
    };

    const renderQueue=items=>{
        if(queueCount)queueCount.textContent=items?.length?`(${items.length})`:"";
        if(!queueList)return;
        if(!items?.length){
            queueList.innerHTML='<div class="download-history-empty">Nothing is waiting for release.</div>';
            return;
        }
        queueList.innerHTML="";
        items.forEach(item=>{
            const row=document.createElement("div");
            row.className=`download-history-row download-queue-row status-${item.status||"waiting"}`;
            const main=document.createElement("div");
            main.className="download-history-main";
            const name=document.createElement("strong");
            name.textContent=item.model_name||item.model_key||"Queued model";
            const version=document.createElement("span");
            const statusLabel=item.status==="ready"?"Ready to install":
                item.status==="installing"?"Installing…":
                item.status==="error"?"Needs attention":"Waiting for release";
            version.textContent=`${sourceLabel(item.source)} • ${item.version_name||"Current version"} • ${statusLabel}`;
            main.append(name,version);
            if(item.release_at){
                const release=document.createElement("span");
                const date=new Date(item.release_at);
                release.textContent=Number.isNaN(date.getTime())?`Release target: ${item.release_at}`:`Release target: ${date.toLocaleDateString()}`;
                main.append(release);
            }
            if(item.last_error){
                const err=document.createElement("span");
                err.className="download-queue-error";
                err.textContent=item.last_error;
                main.append(err);
            }
            row.append(main);

            const actions=document.createElement("div");
            actions.className="download-queue-actions";
            if(item.status==="ready"){
                const install=document.createElement("button");
                install.type="button"; install.className="download-queue-install"; install.textContent="Install";
                install.addEventListener("click",async()=>{
                    install.disabled=true;install.textContent="Starting…";
                    try{
                        const response=await fetch(`/api/download-queue/${item.id}/install`,{method:"POST"});
                        const data=await response.json();
                        if(!response.ok||!data.success)throw new Error(data.error||"Install failed.");
                        await loadQueue();
                    }catch(error){
                        window.modelRadarAlert?.(error.message||"Install failed",{title:"Queued download"});
                        install.disabled=false;install.textContent="Install";
                    }
                });
                actions.append(install);
            }
            const remove=document.createElement("button");
            remove.type="button";remove.className="download-queue-remove";remove.textContent="Remove";
            remove.addEventListener("click",async()=>{
                remove.disabled=true;
                await fetch(`/api/download-queue/${item.id}`,{method:"DELETE"}).catch(()=>{});
                loadQueue();
            });
            actions.append(remove);
            row.append(actions);
            queueList.append(row);
        });
    };

    const loadQueue=async()=>{
        if(!queueList)return;
        queueList.innerHTML='<div class="download-history-empty">Loading…</div>';
        try{
            const response=await fetch("/api/download-queue",{cache:"no-store"});
            const data=await response.json();
            if(!response.ok||!data.success)throw new Error(data.error||"Unable to load queue.");
            renderQueue(data.items||[]);
        }catch(error){
            queueList.innerHTML=`<div class="download-history-empty">${error.message||"Unable to load queue."}</div>`;
        }
    };

    const renderWatchlist=items=>{
        if(watchCount)watchCount.textContent=items?.length?`(${items.length})`:"";
        if(!watchList)return;
        if(!items?.length){
            watchList.innerHTML='<div class="download-history-empty">No paid files are being watched.</div>';
            return;
        }
        watchList.innerHTML="";
        items.forEach(item=>{
            const row=document.createElement("div");
            row.className=`download-history-row download-watch-row status-${item.status||"waiting"}`;
            const main=document.createElement("div");
            main.className="download-history-main";
            const name=document.createElement("button");
            name.type="button";
            name.className="download-history-model-link";
            name.textContent=item.model_name||item.model_key||"Watched model";
            name.title="Open this model in AbyssBeacon";
            name.addEventListener("click",event=>{
                event.stopPropagation();
                overlay.classList.remove("open");
                overlay.setAttribute("aria-hidden","true");
                if(typeof window.modelRadarOpenModel==="function" && window.modelRadarOpenModel(item.model_id,{downloads:true})) return;
                window.modelRadarAlert?.("This model could not be opened.",{title:"Model unavailable"});
            });
            const version=document.createElement("span");
            const statusLabel=item.status==="available"?"✓ Available for download":
                item.status==="error"?"Needs attention":"Watching for availability";
            version.textContent=`${sourceLabel(item.source)} • ${item.version_name||"Current version"} • ${statusLabel}`;
            const file=document.createElement("span");
            file.className="download-watch-file";
            file.textContent=`${item.file_name||"Model file"}${item.file_size_display?` • ${item.file_size_display}`:""}`;
            main.append(name,version,file);
            if(item.available_at){
                const available=document.createElement("span");
                available.className="download-watch-available";
                available.textContent=`Available ${age(item.available_at)}`;
                main.append(available);
            }
            if(item.last_error){
                const err=document.createElement("span");
                err.className="download-queue-error";
                err.textContent=item.last_error;
                main.append(err);
            }
            row.append(main);
            const actions=document.createElement("div");
            actions.className="download-queue-actions";
            if(item.status==="available"){
                const openModel=document.createElement("button");
                openModel.type="button";
                openModel.className="download-queue-install";
                openModel.textContent="Open Model";
                openModel.addEventListener("click",()=>name.click());
                actions.append(openModel);
            }
            const remove=document.createElement("button");
            remove.type="button";remove.className="download-queue-remove";remove.textContent="Remove";
            remove.addEventListener("click",async()=>{
                remove.disabled=true;
                await fetch(`/api/download-watchlist/${item.id}`,{method:"DELETE"}).catch(()=>{});
                loadWatchlist();
            });
            actions.append(remove);
            row.append(actions);
            watchList.append(row);
        });
    };

    const loadWatchlist=async()=>{
        if(!watchList)return;
        watchList.innerHTML='<div class="download-history-empty">Loading…</div>';
        try{
            const response=await fetch("/api/download-watchlist",{cache:"no-store"});
            const data=await response.json();
            if(!response.ok||!data.success)throw new Error(data.error||"Unable to load Watchlist.");
            renderWatchlist(data.items||[]);
        }catch(error){
            watchList.innerHTML=`<div class="download-history-empty">${error.message||"Unable to load Watchlist."}</div>`;
        }
    };

    activeTab?.addEventListener("click",()=>setManagerTab("active"));
    historyTab?.addEventListener("click",()=>setManagerTab("history"));
    queueTab?.addEventListener("click",()=>setManagerTab("queue"));
    watchTab?.addEventListener("click",()=>setManagerTab("watch"));

    const open=async()=>{
        overlay.classList.add("open");
        overlay.setAttribute("aria-hidden","false");
        load();
        loadQueue();
        loadWatchlist();

        // "Active" is the working-download area, so paused/interrupted jobs
        // belong there too. Query the current snapshot when the manager opens
        // instead of relying only on the last background poll; this also makes
        // the first click after restarting Flask choose the correct tab.
        let workCount=Number(window.modelRadarActiveDownloadCount||0);
        try{
            const response=await fetch("/api/active-downloads",{cache:"no-store"});
            const data=await response.json();
            if(response.ok&&data.success){
                const items=Array.isArray(data.items)?data.items:[];
                const workStatuses=new Set([
                    "starting","downloading","installing","canceling","pausing",
                    "paused","failed"
                ]);
                workCount=items.filter(item=>workStatuses.has(String(item.status||""))).length;
                window.modelRadarActiveDownloadCount=workCount;
            }
        }catch(_){}
        setManagerTab(workCount>0?"active":"history");
    };
    const hide=()=>{
        overlay.classList.remove("open");
        overlay.setAttribute("aria-hidden","true");
    };

    button.addEventListener("click",open);
    close?.addEventListener("click",hide);
    overlay.addEventListener("click",event=>{if(event.target===overlay)hide();});
});


// Persistent notifications for paid Watchlist files that became downloadable.
window.modelRadarShowWatchNotifications=async()=>{
    let response,data;
    try{
        response=await fetch("/api/download-watchlist/notifications",{cache:"no-store"});
        data=await response.json();
        if(!response.ok||!data.success)return;
    }catch(_){ return; }
    const items=Array.isArray(data.items)?data.items:[];
    if(!items.length)return;

    let host=document.getElementById("modelRadarToastHost");
    if(!host){
        host=document.createElement("div");
        host.id="modelRadarToastHost";
        host.className="modelradar-toast-host";
        document.body.appendChild(host);
    }
    items.forEach(item=>{
        if(host.querySelector(`[data-watch-notice-id="${item.id}"]`)) return;
        const toast=document.createElement("div");
        toast.className="modelradar-toast watch-available show";
        toast.dataset.watchNoticeId=String(item.id);
        const body=document.createElement("div");
        body.className="watch-toast-body";
        const title=document.createElement("strong");
        title.textContent="Available for download";
        const model=document.createElement("button");
        model.type="button";
        model.className="watch-toast-model";
        model.textContent=item.model_name||item.model_key||"Model";
        const file=document.createElement("span");
        file.textContent=item.file_name||"Watched file";
        model.addEventListener("click",()=>{
            if(typeof window.modelRadarOpenModel==="function" && window.modelRadarOpenModel(item.model_id,{downloads:true})){
                fetch(`/api/download-watchlist/${item.id}/dismiss`,{method:"POST"}).catch(()=>{});
                toast.remove();
            }
        });
        const close=document.createElement("button");
        close.type="button";
        close.className="watch-toast-close";
        close.textContent="×";
        close.setAttribute("aria-label","Dismiss availability notification");
        close.addEventListener("click",async()=>{
            close.disabled=true;
            await fetch(`/api/download-watchlist/${item.id}/dismiss`,{method:"POST"}).catch(()=>{});
            toast.classList.remove("show");
            setTimeout(()=>toast.remove(),220);
        });
        body.append(title,model,file);
        toast.append(body,close);
        host.appendChild(toast);
    });
};
document.addEventListener("DOMContentLoaded",()=>window.modelRadarShowWatchNotifications?.());


// Live Local Installer download queue.
document.addEventListener("DOMContentLoaded",()=>{
    const ping=document.getElementById("activeDownloadPing");
    const managerButton=document.getElementById("downloadHistoryButton");
    const list=document.getElementById("activeDownloadsList");
    const count=document.getElementById("downloadActiveCount");
    if(!list)return;

    let lastSignature="";
    let timer=null;

    const humanBytes=value=>{
        const n=Math.max(0,Number(value)||0);
        if(n<1024)return `${Math.round(n)} B`;
        const units=["KB","MB","GB","TB"]; let x=n/1024,unit=0;
        while(x>=1024&&unit<units.length-1){x/=1024;unit++;}
        const digits=x>=100?0:x>=10?1:2;
        return `${x.toFixed(digits)} ${units[unit]}`;
    };
    const sourceLabel=value=>({
        huggingface:"Hugging Face",modelscope:"ModelScope",civitai:"CivitAI",
        civitaired:"CivitAI Red",tensorhub:"TensorHub Art",seaart:"SeaArt"
    }[String(value||"").toLowerCase()]||value||"Source");

    const render=data=>{
        const items=Array.isArray(data.items)?data.items:[];
        const workStatuses=new Set([
            "starting","downloading","installing","canceling","pausing",
            "paused","failed"
        ]);
        // Count every unfinished/resumable job in the Active tab. A paused or
        // interrupted transfer still represents work the user has in progress,
        // even though bytes are not moving at this exact moment.
        const active=items.filter(item=>workStatuses.has(String(item.status||""))).length;
        const failed=Number(data.failed_count||0);
        window.modelRadarActiveDownloadCount=active;
        if(count) count.textContent=active>0?`(${active})`:"";
        if(managerButton){
            managerButton.classList.toggle("has-active-download", active>0);
        }
        if(ping){
            ping.hidden=active<=0;
            ping.title=active===1?"1 download in Active":`${active} downloads in Active`;
            ping.setAttribute("aria-label",ping.title);
        }

        if(!items.length){
            list.innerHTML='<div class="active-downloads-empty">No active downloads.</div>';
            return;
        }
        list.innerHTML="";
        items.forEach(item=>{
            const row=document.createElement("div");
            row.className=`active-download-row status-${item.status||"starting"}`;

            const head=document.createElement("div");
            head.className="active-download-head";
            const names=document.createElement("div");
            names.className="active-download-names";
            const title=document.createElement("strong");
            title.textContent=item.model_name||"Model";
            title.title=title.textContent;
            const file=document.createElement("span");
            file.textContent=`${sourceLabel(item.source)} · ${item.filename||"Model file"}`;
            file.title=file.textContent;
            names.append(title,file);

            const headActions=document.createElement("div");
            headActions.className="active-download-head-actions";
            const status=document.createElement("span");
            status.className="active-download-status";
            status.textContent=item.status==="failed"?"Interrupted":
                item.status==="paused"?"Paused":
                item.status==="pausing"?"Pausing…":
                item.status==="complete"?"Complete":
                item.status==="canceled"?"Canceled":
                item.status==="canceling"?"Canceling…":
                item.stage||"Downloading";
            headActions.append(status);

            if(["starting","downloading"].includes(item.status)){
                const pause=document.createElement("button");
                pause.type="button";
                pause.className="active-download-retry";
                pause.textContent="Ⅱ";
                pause.title="Pause download";
                pause.setAttribute("aria-label",`Pause ${item.filename||"download"}`);
                pause.addEventListener("click",async()=>{
                    if(pause.disabled)return;
                    pause.disabled=true;
                    status.textContent="Pausing…";
                    try{
                        const response=await fetch(`/api/active-downloads/${encodeURIComponent(item.id)}/pause`,{method:"POST"});
                        const data=await response.json().catch(()=>({}));
                        if(!response.ok||!data.success)throw new Error(data.error||"Unable to pause download.");
                    }catch(error){
                        pause.disabled=false;
                        window.modelRadarAlert?.(error.message||"Unable to pause download",{title:"Pause failed"});
                    }finally{ poll(); }
                });
                headActions.append(pause);
            }
            if(["starting","downloading","installing"].includes(item.status)){
                const cancel=document.createElement("button");
                cancel.type="button";
                cancel.className="active-download-cancel";
                cancel.textContent="×";
                cancel.title="Cancel and discard partial download";
                cancel.setAttribute("aria-label",`Cancel ${item.filename||"download"}`);
                cancel.addEventListener("click",async()=>{
                    if(cancel.disabled)return;
                    cancel.disabled=true;
                    status.textContent="Canceling…";
                    try{
                        const response=await fetch(`/api/active-downloads/${encodeURIComponent(item.id)}/cancel`,{method:"POST"});
                        const data=await response.json().catch(()=>({}));
                        if(!response.ok||!data.success)throw new Error(data.error||"Unable to cancel download.");
                    }catch(error){
                        cancel.disabled=false;
                        window.modelRadarAlert?.(error.message||"Unable to cancel download",{title:"Cancel failed"});
                    }finally{ poll(); }
                });
                headActions.append(cancel);
            }

            head.append(names,headActions);
            row.append(head);

            const track=document.createElement("div");
            track.className="active-download-track";
            const bar=document.createElement("div");
            bar.className="active-download-bar";
            const pct=Number(item.percent);
            if(Number.isFinite(pct))bar.style.width=`${Math.max(0,Math.min(100,pct))}%`;
            else if(!["failed","paused","canceled","canceling","pausing"].includes(item.status))bar.classList.add("indeterminate");
            track.append(bar);
            row.append(track);

            const foot=document.createElement("div");
            foot.className="active-download-foot";
            const meta=document.createElement("span");
            if(item.status==="failed"){
                meta.textContent=item.error||"Connection interrupted. Resume to continue the partial file.";
                meta.title=meta.textContent;
            }else if(item.status==="paused"){
                const downloaded=humanBytes(item.downloaded_bytes);
                const total=Number(item.total_bytes)>0?humanBytes(item.total_bytes):"size unknown";
                meta.textContent=`${downloaded} / ${total} · partial file saved on disk`;
            }else if(item.status==="pausing"){
                meta.textContent="Finishing current chunk and saving partial file…";
            }else if(item.status==="complete"){
                meta.textContent="Installed successfully";
            }else if(item.status==="canceled"){
                meta.textContent="Download canceled · partial file removed";
            }else if(item.status==="canceling"){
                meta.textContent="Stopping transfer…";
            }else{
                const downloaded=humanBytes(item.downloaded_bytes);
                const total=Number(item.total_bytes)>0?humanBytes(item.total_bytes):"size unknown";
                const percent=Number.isFinite(pct)?` · ${Math.round(pct)}%`:"";
                const speed=Number(item.speed_bps)>0?` · ${humanBytes(item.speed_bps)}/s`:"";
                meta.textContent=`${downloaded} / ${total}${percent}${speed}`;
            }
            foot.append(meta);

            if(["failed","paused"].includes(item.status)){
                const actions=document.createElement("div");
                actions.className="active-download-actions";
                if(item.retry_url){
                    const retry=document.createElement("button");
                    retry.type="button"; retry.className="active-download-retry";
                    retry.textContent="▶"; retry.title="Resume download";
                    retry.addEventListener("click",async()=>{
                        retry.disabled=true;
                        try{
                            const joiner=item.retry_url.includes("?")?"&":"?";
                            const response=await fetch(`${item.retry_url}${joiner}resume=1&job_id=${encodeURIComponent(item.id)}`,{headers:{"Accept":"application/json"}});
                            const raw=await response.text();
                            let payload={}; try{payload=raw?JSON.parse(raw):{};}catch(_){}
                            if(!response.ok||!payload.success){
                                if(payload.paused===true||payload.canceled===true)return;
                                throw new Error(payload.error||raw||`Resume failed (HTTP ${response.status})`);
                            }
                        }catch(error){
                            const message=String(error?.message||"Resume failed");
                            // When AbyssBeacon/Flask or the network disappears while a
                            // resumed fetch is open, the saved .part remains intact and
                            // startup restores this same job as Paused. Do not show a
                            // failure modal for that expected interruption.
                            if(/networkerror|failed to fetch|network request failed|load failed/i.test(message)){
                                return;
                            }
                            window.modelRadarAlert?.(message,{title:"Download failed"});
                        }finally{
                            retry.disabled=false;
                            poll();
                        }
                    });
                    actions.append(retry);
                }
                const dismiss=document.createElement("button");
                dismiss.type="button"; dismiss.className="active-download-dismiss";
                dismiss.textContent="×"; dismiss.title="Cancel and discard partial download";
                dismiss.setAttribute("aria-label",`Discard ${item.filename||"download"}`);
                dismiss.addEventListener("click",async()=>{
                    if(dismiss.disabled)return;
                    dismiss.disabled=true;
                    try{
                        const response=await fetch(`/api/active-downloads/${encodeURIComponent(item.id)}/discard`,{method:"POST"});
                        const data=await response.json().catch(()=>({}));
                        if(!response.ok||!data.success)throw new Error(data.error||"Unable to discard partial download.");
                    }catch(error){
                        dismiss.disabled=false;
                        window.modelRadarAlert?.(error.message||"Unable to discard partial download.",{title:"Cancel failed"});
                    }finally{ poll(); }
                });
                actions.append(dismiss);
                foot.append(actions);
            }
            row.append(foot);
            list.append(row);
        });
    };

    const poll=async()=>{
        try{
            const response=await fetch("/api/active-downloads",{cache:"no-store"});
            const data=await response.json();
            if(!response.ok||!data.success)return;
            const signature=JSON.stringify(data);
            if(signature!==lastSignature){lastSignature=signature;render(data);}
        }catch(_){}
    };

    window.modelRadarPollActiveDownloads=poll;
    poll();
    timer=setInterval(poll,700);
});
