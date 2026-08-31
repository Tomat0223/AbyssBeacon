
window.modelRadarAlert = window.modelRadarAlert || function(message, options={}){
    return new Promise(resolve=>{
        const old=document.getElementById("modelRadarAlertOverlay");
        if(old) old.remove();

        const overlay=document.createElement("div");
        overlay.id="modelRadarAlertOverlay";
        overlay.className="modelradar-confirm-overlay";

        const box=document.createElement("div");
        box.className="modelradar-confirm-box";

        const title=document.createElement("h3");
        title.textContent=options.title || "AbyssBeacon";

        const body=document.createElement("p");
        body.textContent=message;

        const actions=document.createElement("div");
        actions.className="modelradar-confirm-actions";

        const finish=()=>{
            overlay.classList.remove("open");
            setTimeout(()=>overlay.remove(),130);
            resolve();
        };

        if(options.linkUrl){
            const link=document.createElement("a");
            link.className="modelradar-confirm-cancel";
            link.href=options.linkUrl;
            link.target="_blank";
            link.rel="noopener";
            link.textContent=options.linkText || "Open source";
            link.addEventListener("click",()=>setTimeout(finish,0));
            actions.append(link);
        }

        const ok=document.createElement("button");
        ok.type="button";
        ok.className="modelradar-confirm-ok";
        ok.textContent=options.okText || "OK";

        ok.addEventListener("click",finish);
        overlay.addEventListener("click",event=>{
            if(event.target===overlay) finish();
        });

        actions.append(ok);
        box.append(title,body,actions);
        overlay.append(box);
        document.body.append(overlay);
        requestAnimationFrame(()=>overlay.classList.add("open"));
        ok.focus();
    });
};

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


function showModelRadarToast(message, kind="success"){
    let host=document.getElementById("modelRadarToastHost");
    if(!host){
        host=document.createElement("div");
        host.id="modelRadarToastHost";
        host.className="modelradar-toast-host";
        document.body.appendChild(host);
    }
    const toast=document.createElement("div");
    toast.className=`modelradar-toast ${kind}`;
    toast.textContent=message;
    host.appendChild(toast);
    requestAnimationFrame(()=>toast.classList.add("show"));
    setTimeout(()=>{
        toast.classList.remove("show");
        setTimeout(()=>toast.remove(),220);
    },4200);
}

function updateFavoritePeers(modelId, favorite){
    const id = String(modelId || "").trim();
    if(!id) return;

    document.querySelectorAll(
        `.model-favorite-btn[data-model-id="${CSS.escape(id)}"]`
    ).forEach(peer => {
        peer.dataset.favorite = favorite ? "true" : "false";
        peer.classList.toggle("is-favorite", !!favorite);
        peer.textContent = favorite ? "★" : "☆";
        peer.title = favorite ? "Remove from favorites" : "Favorite model";
        peer.setAttribute(
            "aria-label",
            favorite ? "Remove from favorites" : "Favorite model"
        );
    });

    document.querySelectorAll(
        `.model-card[data-id="${CSS.escape(id)}"]`
    ).forEach(card => {
        card.dataset.favorite = favorite ? "true" : "false";
    });

    if(typeof window.modelRadarFilterCards === "function"){
        window.modelRadarFilterCards();
    }else if(typeof filterCards === "function"){
        filterCards();
    }
}


/**
 * Favorites use one delegated handler instead of binding every star button.
 *
 * Feed Windowing can append/replace cards at any time, so per-button event
 * listeners become stale. Delegation makes every current and future
 * .model-favorite-btn work automatically, including modal/detail favorites.
 */
function bindFavoriteButtons(root = document) {
    const eventRoot = root === document ? document : document;

    if(eventRoot.dataset?.modelRadarFavoriteDelegated === "true") return;

    // document has no dataset in some browser implementations, so keep the
    // guard on a private property too.
    if(eventRoot.__modelRadarFavoriteDelegated === true) return;
    eventRoot.__modelRadarFavoriteDelegated = true;

    eventRoot.addEventListener("click", async event => {
        const button = event.target.closest(".model-favorite-btn");
        if(!button) return;

        event.preventDefault();
        event.stopPropagation();

        if(button.dataset.favoritePending === "true") return;

        const id = String(button.dataset.modelId || "").trim();
        if(!id) return;

        const next = button.dataset.favorite !== "true";
        button.dataset.favoritePending = "true";

        try {
            const response = await fetch(`/model/${encodeURIComponent(id)}/favorite`, {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({favorite: next})
            });

            const data = await response.json();
            if(!response.ok || !data.success){
                throw new Error(data.error || "Unable to update favorite.");
            }

            updateFavoritePeers(id, !!data.favorite);

        } catch (error) {
            console.error("Favorite update failed", error);
        } finally {
            delete button.dataset.favoritePending;
        }
    });
}


function markModelRadarCardDownloaded(modelId){
    const id=String(modelId||"").trim();
    if(!id)return;

    document.querySelectorAll(`.model-card[data-id="${CSS.escape(id)}"]`).forEach(card=>{
        card.dataset.downloaded="true";
        card.dataset.update="false";

        let badge=card.querySelector(".card-download-state");
        if(!badge){
            badge=document.createElement("span");
            badge.className="card-download-state";
            const image=card.querySelector(".card-image");
            if(image)image.appendChild(badge);
        }

        badge.classList.remove("is-update");
        badge.classList.add("is-current");
        badge.textContent="✓";
        badge.dataset.tooltip="Downloaded · current version";
        badge.setAttribute("role","img");
        badge.setAttribute("aria-label","Downloaded current version");
    });
}


function initializeModal(){

    bindFavoriteButtons(document);

    const cards =
        document.querySelectorAll(".model-card");


    const overlay =
        document.getElementById("modelOverlay");


    const details =
        document.getElementById("modelDetails");


    if(!overlay || !details){

        return;

    }

    let activeGalleryCleanup = null;

    function cleanupActiveGallery(){
        if(typeof activeGalleryCleanup === "function"){
            try{ activeGalleryCleanup(); }catch(e){}
        }
        activeGalleryCleanup = null;
    }

    function stopDetailMedia(){
        details.querySelectorAll("video").forEach(video=>{
            try{
                video.pause();
                video.currentTime=0;
            }catch(e){}
        });
    }

    function closeModelOverlay(){
        cleanupActiveGallery();
        stopDetailMedia();
        overlay.classList.remove("open");
        const panel=document.querySelector(".model-panel");
        if(panel){
            panel.scrollTop=0;
            panel.style.removeProperty("--detail-source-color");
            delete panel.dataset.source;
        }
        details.scrollTop=0;
        details.replaceChildren();
        document.body.style.overflow="";
    }

    // Other first-party UI surfaces (for example Download Manager history)
    // can open a model through the exact same card click lifecycle instead of
    // maintaining a second copy of the modal initialization logic.
    window.modelRadarOpenModel = (modelId, options={}) => {
        let card = document.querySelector(`.model-card[data-id="${String(modelId)}"]`);
        let temporary = false;
        if(!card){
            const grid=document.querySelector(".feed-grid");
            if(!grid) return false;
            card=document.createElement("article");
            card.className="model-card";
            card.dataset.id=String(modelId);
            card.hidden=true;
            grid.appendChild(card);
            temporary=true;
        }
        let target=card;
        let shortcut=null;
        if(options?.downloads){
            shortcut=document.createElement("span");
            shortcut.className="card-access-icon is-downloadable";
            shortcut.hidden=true;
            card.appendChild(shortcut);
            target=shortcut;
        }
        target.dispatchEvent(new MouseEvent("click", {bubbles:true, cancelable:true, view:window}));
        if(shortcut) setTimeout(()=>shortcut.remove(),0);
        if(temporary) setTimeout(()=>card.remove(),0);
        return true;
    };


    const feedGrid = document.querySelector(".feed-grid");
    feedGrid?.addEventListener("click", (event) => {
        const card = event.target.closest(".model-card");
        if(!card || !feedGrid.contains(card)) return;

                const openDownloadsFromCard = Boolean(
                    event.target.closest(".card-access-icon.is-downloadable")
                );

                // Let real links/buttons inside the card perform their own
                // action. In particular, author badges navigate to the
                // Creator view instead of opening the model underneath them.
                if (event.target.closest("a, button, input, select, textarea, label")) {
                    return;
                }

                event.preventDefault();


                const wasNew =
                    String(card.dataset.status || "").toLowerCase() === "new"
                    || Boolean(card.querySelector(".badge-new"));


                const id =
                    card.dataset.id;

                if (String(card.dataset.type || "").trim().toLowerCase() === "collection") {
                    window.location.assign(`/collection/${encodeURIComponent(id)}`);
                    return;
                }

                cleanupActiveGallery();
                stopDetailMedia();

                const maturityModeForDetail = document.getElementById("sensitiveFilter")?.value || "hide";
                fetch(`/model/${id}?mature=${encodeURIComponent(maturityModeForDetail)}`)

                    .then(response => response.text())

                    .then(html => {


                        details.innerHTML = html;

                        // /model/<id> marks the database row viewed. Mirror that
                        // successful transition through the shared live Seen
                        // state API so the navbar/window never drifts stale.
                        if (
                            wasNew
                            && typeof window.modelRadarApplySeenState === "function"
                        ) {
                            window.modelRadarApplySeenState(
                                [card],
                                {
                                    changed:1,
                                    refreshNewWindow:true
                                }
                            ).catch(error => {
                                console.error("Unable to sync opened card Seen state:", error);
                            });
                        }

                        // The visible rounded frame belongs to .model-panel,
                        // while the source color arrives on the injected
                        // .model-detail element. CSS variables do not inherit
                        // upward, so explicitly copy the source color to the
                        // actual frame whenever a model is opened.
                        const detailRoot = details.querySelector(".model-detail");
                        const detailPanel = overlay.querySelector(".model-panel");

                        // Blur mature media inside the detail/gallery whenever
                        // the persistent Maturity preference is set to Blur.
                        // The setting remains controlled only from Settings.
                        if (detailRoot) {
                            const maturityMode = document.getElementById("sensitiveFilter")?.value || "hide";
                            const isSensitive = String(detailRoot.dataset.sensitive || "false").toLowerCase() === "true";
                            detailRoot.classList.toggle(
                                "sensitive-blurred",
                                isSensitive && maturityMode === "blur"
                            );
                        }
                        if (detailPanel && detailRoot) {
                            const inlineSourceColor =
                                detailRoot.style.getPropertyValue("--source-color").trim();
                            const cardSourceColor =
                                getComputedStyle(card).getPropertyValue("--source-color").trim();
                            const sourceColor =
                                inlineSourceColor || cardSourceColor || "#00eaff";

                            detailPanel.style.setProperty(
                                "--detail-source-color",
                                sourceColor
                            );
                            detailPanel.dataset.source =
                                detailRoot.dataset.source || card.dataset.source || "";
                        }


                        const formatManagedFileSize = bytes => {
                            const value = Number(bytes || 0);
                            if (!Number.isFinite(value) || value <= 0) return "";
                            const units = ["B", "KB", "MB", "GB", "TB"];
                            let amount = value;
                            let unit = 0;
                            while (amount >= 1024 && unit < units.length - 1) {
                                amount /= 1024;
                                unit += 1;
                            }
                            return `${amount >= 10 || unit === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[unit]}`;
                        };

                        const chooseManagedDownloads = ({title, note, items, actionText, destructive=false}) => new Promise(resolve => {
                            const overlay = document.createElement("div");
                            overlay.className = "download-file-picker-overlay";
                            const dialog = document.createElement("div");
                            dialog.className = "download-file-picker";
                            const safeItems = Array.isArray(items) ? items : [];
                            dialog.innerHTML = `
                                <div class="download-file-picker-head">
                                    <strong>${title}</strong>
                                    <button type="button" class="download-file-picker-close" aria-label="Close">×</button>
                                </div>
                                <div class="download-file-picker-note"></div>
                                <div class="download-file-picker-list"></div>
                                <div class="download-file-picker-footer">
                                    <button type="button" class="download-file-picker-select-all">Select all</button>
                                    <div class="download-file-picker-footer-spacer"></div>
                                    <button type="button" class="download-file-picker-cancel">Cancel</button>
                                    <button type="button" class="download-file-picker-apply${destructive ? " destructive" : ""}">${actionText}</button>
                                </div>`;
                            dialog.querySelector(".download-file-picker-note").textContent = note || "";
                            const list = dialog.querySelector(".download-file-picker-list");
                            safeItems.forEach((item, index) => {
                                const row = document.createElement("label");
                                row.className = "download-file-picker-row";
                                const size = formatManagedFileSize(item.size_bytes);
                                const version = String(item.version_name || "").trim();
                                row.innerHTML = `<input type="checkbox" value="${String(item.id)}" checked><span><strong></strong><small></small></span>`;
                                row.querySelector("strong").textContent = item.filename || `Tracked file ${index + 1}`;
                                row.querySelector("small").textContent = [version, size].filter(Boolean).join(" · ");
                                list.appendChild(row);
                            });
                            const finish = value => {
                                overlay.remove();
                                resolve(value);
                            };
                            dialog.querySelector(".download-file-picker-close").addEventListener("click", ()=>finish(null));
                            dialog.querySelector(".download-file-picker-cancel").addEventListener("click", ()=>finish(null));
                            overlay.addEventListener("click", event => { if (event.target === overlay) finish(null); });
                            dialog.querySelector(".download-file-picker-select-all").addEventListener("click", () => {
                                const boxes = [...list.querySelectorAll('input[type="checkbox"]')];
                                const shouldCheck = boxes.some(box => !box.checked);
                                boxes.forEach(box => { box.checked = shouldCheck; });
                            });
                            dialog.querySelector(".download-file-picker-apply").addEventListener("click", () => {
                                const selected = [...list.querySelectorAll('input[type="checkbox"]:checked')]
                                    .map(box => Number(box.value)).filter(Number.isFinite);
                                if (!selected.length) return;
                                finish(selected);
                            });
                            overlay.appendChild(dialog);
                            document.body.appendChild(overlay);
                        });

                        const sidecarOptions = details.querySelector(".download-sidecar-options");
                        const saveInfoForDownload = details.querySelector(".download-save-info");
                        const savePreviewForDownload = details.querySelector(".download-save-preview");
                        const localDownloadMode = String(window.userPreferences?.download_behavior || "browser").toLowerCase() === "local";
                        if (sidecarOptions) sidecarOptions.hidden = !localDownloadMode;
                        if (saveInfoForDownload) saveInfoForDownload.checked = window.userPreferences?.save_model_info !== false;
                        if (savePreviewForDownload) savePreviewForDownload.checked = window.userPreferences?.save_model_preview !== false;

                        const clearDownloadHistory = details.querySelector(".clear-model-download-history");
                        if (clearDownloadHistory) {
                            clearDownloadHistory.addEventListener("click", async event => {
                                event.preventDefault();
                                event.stopPropagation();
                                const modelId = clearDownloadHistory.dataset.modelId;
                                const modelName = clearDownloadHistory.dataset.modelName || "this model";
                                if (!modelId) return;
                                try {
                                    const previewResponse = await fetch(`/api/download-history/model/${modelId}`);
                                    const previewData = await previewResponse.json();
                                    if (!previewResponse.ok || !previewData.success) throw new Error(previewData.error || "Unable to inspect download history.");
                                    const records = previewData.files || [];
                                    if (!records.length) {
                                        await window.modelRadarAlert("AbyssBeacon does not have download history for this model.", {title:"Remove from History"});
                                        return;
                                    }
                                    let selectedIds;
                                    if (records.length === 1) {
                                        const filename = records[0].filename || modelName;
                                        if (!(await window.modelRadarConfirm(
                                            `Remove "${filename}" from AbyssBeacon's download history? The file on disk will not be touched.`,
                                            {title:"Remove from History?", okText:"Remove from history"}
                                        ))) return;
                                        selectedIds = [records[0].id];
                                    } else {
                                        selectedIds = await chooseManagedDownloads({
                                            title:"Remove from History",
                                            note:`Choose which tracked downloads for "${modelName}" AbyssBeacon should forget. Files on disk will not be touched.`,
                                            items:records,
                                            actionText:"Remove selected"
                                        });
                                        if (!selectedIds) return;
                                    }
                                    clearDownloadHistory.disabled = true;
                                    const response = await fetch(`/api/download-history/model/${modelId}`, {
                                        method:"DELETE",
                                        headers:{"Content-Type":"application/json"},
                                        body:JSON.stringify({history_ids:selectedIds})
                                    });
                                    const data = await response.json();
                                    if (!response.ok || !data.success) throw new Error(data.error || "Unable to clear download history.");
                                    showModelRadarToast(`Removed ${selectedIds.length} download${selectedIds.length === 1 ? "" : "s"} from history`);
                                    // Reload the current model detail in place so the Downloads
                                    // panel reflects the new history state without kicking the
                                    // user back to the feed or leaving stale management data.
                                    setTimeout(() => {
                                        if (typeof window.modelRadarOpenModel === "function") {
                                            window.modelRadarOpenModel(modelId, {downloads:true});
                                        }
                                    }, 180);
                                } catch (error) {
                                    await window.modelRadarAlert(error.message || "Unable to clear download history.", {title:"AbyssBeacon"});
                                    clearDownloadHistory.disabled = false;
                                }
                            });
                        }


                        const deleteLocalFiles = details.querySelector(".delete-model-local-files");
                        if (deleteLocalFiles) {
                            deleteLocalFiles.addEventListener("click", async event => {
                                event.preventDefault();
                                event.stopPropagation();
                                const modelId = deleteLocalFiles.dataset.modelId;
                                const modelName = deleteLocalFiles.dataset.modelName || "this model";
                                if (!modelId) return;
                                try {
                                    const previewResponse = await fetch(`/api/installed-files/model/${modelId}`);
                                    const previewData = await previewResponse.json();
                                    if (!previewResponse.ok || !previewData.success) throw new Error(previewData.error || "Unable to inspect local files.");
                                    const existing = (previewData.files || []).filter(item => item.exists);
                                    if (!existing.length) {
                                        await window.modelRadarAlert("AbyssBeacon does not have any recorded local files for this model.", {title:"Delete Local Files"});
                                        return;
                                    }
                                    let selectedIds;
                                    if (existing.length === 1) {
                                        selectedIds = [existing[0].id];
                                    } else {
                                        selectedIds = await chooseManagedDownloads({
                                            title:"Delete Local Files",
                                            note:`Choose which recorded files for "${modelName}" to permanently delete. Shared AbyssBeacon sidecars are kept while another tracked file remains in the folder.`,
                                            items:existing,
                                            actionText:"Continue",
                                            destructive:true
                                        });
                                        if (!selectedIds) return;
                                    }

                                    const selectedFiles = existing.filter(item => selectedIds.includes(Number(item.id)));
                                    const selectedNames = selectedFiles.map(item => item.filename || "Recorded file");
                                    const fileList = selectedNames.map(name => `• ${name}`).join("\n");
                                    const sidecarNote = selectedFiles.length === existing.length
                                        ? "\n\nIf these are the last AbyssBeacon-tracked model files in their folder, the shared AbyssBeacon Info.txt and preview image will also be deleted."
                                        : "\n\nShared AbyssBeacon sidecars will be kept while another tracked model file remains in the folder.";
                                    if (!(await window.modelRadarConfirm(
                                        `Permanently delete the following file${selectedFiles.length === 1 ? "" : "s"} from disk?\n\n${fileList}${sidecarNote}`,
                                        {title:"Confirm Local File Deletion", okText:selectedFiles.length === 1 ? "Delete file" : "Delete files"}
                                    ))) return;

                                    deleteLocalFiles.disabled = true;
                                    const response = await fetch(`/api/installed-files/model/${modelId}`, {
                                        method:"DELETE",
                                        headers:{"Content-Type":"application/json"},
                                        body:JSON.stringify({file_ids:selectedIds})
                                    });
                                    const data = await response.json();
                                    if (!response.ok || !data.success) throw new Error(data.error || "Unable to delete local files.");
                                    showModelRadarToast(`Deleted ${selectedIds.length} local file${selectedIds.length === 1 ? "" : "s"}`);
                                    // Refresh this same model in place. This keeps the card open,
                                    // rebuilds installed/history state from the backend, and avoids
                                    // the stale-state path seen when Delete follows Remove History.
                                    setTimeout(() => {
                                        if (typeof window.modelRadarOpenModel === "function") {
                                            window.modelRadarOpenModel(modelId, {downloads:true});
                                        }
                                    }, 180);
                                } catch (error) {
                                    await window.modelRadarAlert(error.message || "Unable to delete local files.", {title:"AbyssBeacon"});
                                    deleteLocalFiles.disabled = false;
                                }
                            });
                        }

                        // DOWNLOAD PANEL

                        const downloadButton =
                            details.querySelector(".download-btn");


                        const downloadOverlay =
                            details.querySelector(".download-overlay");


                        const downloadClose =
                            details.querySelector(".download-close");


                        const showAllDownloads =
                            details.querySelector(".show-all-downloads");


                        const allDownloads =
                            details.querySelector(".all-downloads");


                        if (downloadButton && downloadOverlay) {

                            downloadButton.addEventListener(
                                "click",
                                (event) => {

                                    event.stopPropagation();

                                    clearVersionFilter();
                                    downloadOverlay.classList.add(
                                        "open"
                                    );
                                    requestAnimationFrame(resetDownloadScroll);

                                }
                            );

                        }


                        if (downloadClose && downloadOverlay) {

                            downloadClose.addEventListener(
                                "click",
                                (event) => {

                                    event.stopPropagation();

                                    downloadOverlay.classList.remove(
                                        "open"
                                    );
                                    if (versionDownload) {
                                        versionDownload.classList.remove("active");
                                        versionDownload.textContent = "Show Downloads";
                                    }
                                    setReloadModelVisible(false);

                                }
                            );

                        }



                        // VERSION PILLS
                        // A version is a view state for the whole model detail:
                        // gallery + metadata summary + the download chooser.
                        const versionPills = details.querySelectorAll(".model-version-pill[data-version-name]");
                        const versionRow = details.querySelector(".model-version-row");
                        const versionScrollLeft = details.querySelector(".model-version-scroll-left");
                        const versionScrollRight = details.querySelector(".model-version-scroll-right");
                        const updateVersionScrollControls = () => {
                            if (!versionRow || !versionScrollLeft || !versionScrollRight) return;
                            const maxScroll = Math.max(0, versionRow.scrollWidth - versionRow.clientWidth);
                            const overflow = maxScroll > 2;
                            details.querySelector(".detail-version-nav")?.classList.toggle("versions-overflowing", overflow);
                            versionScrollLeft.hidden = !overflow;
                            versionScrollRight.hidden = !overflow;
                            versionScrollLeft.disabled = !overflow || versionRow.scrollLeft <= 2;
                            versionScrollRight.disabled = !overflow || versionRow.scrollLeft >= maxScroll - 2;
                        };
                        const scrollVersions = direction => {
                            if (!versionRow) return;
                            const amount = Math.max(180, Math.round(versionRow.clientWidth * 0.72));
                            versionRow.scrollBy({left: direction * amount, behavior:"smooth"});
                        };
                        versionScrollLeft?.addEventListener("click", () => scrollVersions(-1));
                        versionScrollRight?.addEventListener("click", () => scrollVersions(1));
                        versionRow?.addEventListener("scroll", updateVersionScrollControls, {passive:true});
                        versionRow?.addEventListener("wheel", event => {
                            if (versionRow.scrollWidth <= versionRow.clientWidth + 2) return;
                            if (Math.abs(event.deltaY) > Math.abs(event.deltaX)) {
                                event.preventDefault();
                                versionRow.scrollLeft += event.deltaY;
                            }
                        }, {passive:false});
                        requestAnimationFrame(updateVersionScrollControls);
                        setTimeout(updateVersionScrollControls, 120);
                        window.addEventListener("resize", updateVersionScrollControls, {passive:true});

                        const versionGroups = details.querySelectorAll(".download-version-group[data-version-name]");
                        const allDownloadFiles = details.querySelectorAll(".download-file");
                        const additionalDownloadFiles = details.querySelectorAll('.download-file[data-download-primary="false"]');
                        const additionalFileSections = details.querySelectorAll(".download-extra-files-section");
                        const downloadContent = details.querySelector(".download-content");
                        const downloadPanel = details.querySelector(".download-panel");
                        const resetDownloadScroll = () => {
                            if (downloadPanel) downloadPanel.scrollTop = 0;
                            if (downloadContent) downloadContent.scrollTop = 0;
                        };
                        const versionSummary = details.querySelector(".model-version-summary-text");
                        const versionDownload = details.querySelector(".model-version-download");
                        const detailArchitectureBadge = details.querySelector(".detail-info .badge.architecture");
                        const defaultDetailArchitecture = String(detailArchitectureBadge?.textContent || "").trim();
                        const reloadModelButtons = details.querySelectorAll(".source-refresh-download-btn");
                        const setReloadModelVisible = (visible) => {
                            reloadModelButtons.forEach(button => {
                                button.hidden = !visible;
                            });
                        };
                        setReloadModelVisible(Boolean(downloadOverlay?.classList.contains("open")));
                        let versionChoices = [];
                        try {
                            const versionJson = details.querySelector(".version-choices-json");
                            versionChoices = versionJson ? JSON.parse(versionJson.textContent || "[]") : [];
                        } catch (e) { versionChoices = []; }
                        let selectedVersionName = "";
                        let selectedVersionId = "";
                        let filterBanner = null;

                        const sourceViewSelect = details.querySelector(".media-source-view-select");
                        let sourceContexts = {};
                        try {
                            const sourceJson = details.querySelector(".source-contexts-json");
                            sourceContexts = sourceJson ? JSON.parse(sourceJson.textContent || "{}") : {};
                        } catch (e) { sourceContexts = {}; }
                        let activeSourceView = "combined";

                        const copyLinkTarget = details.querySelector(".copy-model-link-title");
                        const copyLinkHint = copyLinkTarget?.querySelector(".copy-model-link-title-hint");
                        const defaultCopyLink = String(
                            copyLinkTarget?.dataset.defaultUrl ||
                            copyLinkTarget?.dataset.copyUrl ||
                            ""
                        ).trim();

                        const setCopyLinkTarget = (choice) => {
                            if (!copyLinkTarget) return;
                            const sourceContext = sourceContexts?.[activeSourceView] || sourceContexts?.combined || {};
                            const sourceVersionUrl = activeSourceView !== "combined"
                                ? String(choice?.source_share_urls?.[activeSourceView] || choice?.share_url || "").trim()
                                : String(choice?.share_url || "").trim();
                            const sourceDefaultUrl = String(sourceContext?.url || defaultCopyLink).trim();
                            const target = sourceVersionUrl || sourceDefaultUrl || defaultCopyLink;
                            copyLinkTarget.dataset.copyUrl = target;
                            const versionName = String(choice?.name || "").trim();
                            copyLinkTarget.setAttribute(
                                "aria-label",
                                sourceVersionUrl && versionName
                                    ? `Copy URL for ${versionName}`
                                    : "Copy model URL"
                            );
                        };

                        const writeClipboardText = async (value) => {
                            const text = String(value || "").trim();
                            if (!text) throw new Error("No model link is available to copy.");

                            if (navigator.clipboard?.writeText && window.isSecureContext) {
                                await navigator.clipboard.writeText(text);
                                return;
                            }

                            const textarea = document.createElement("textarea");
                            textarea.value = text;
                            textarea.setAttribute("readonly", "");
                            textarea.style.position = "fixed";
                            textarea.style.opacity = "0";
                            textarea.style.pointerEvents = "none";
                            document.body.appendChild(textarea);
                            textarea.select();
                            textarea.setSelectionRange(0, textarea.value.length);
                            const copied = document.execCommand("copy");
                            textarea.remove();
                            if (!copied) throw new Error("The browser did not allow clipboard access.");
                        };

                        if (copyLinkTarget) {
                            copyLinkTarget.addEventListener("click", async event => {
                                event.preventDefault();
                                event.stopPropagation();
                                if (copyLinkTarget.dataset.copying === "true") return;

                                copyLinkTarget.dataset.copying = "true";
                                try {
                                    await writeClipboardText(copyLinkTarget.dataset.copyUrl);
                                    copyLinkTarget.classList.add("copied");
                                    if (copyLinkHint) copyLinkHint.textContent = "Copied!";
                                    showModelRadarToast("Link copied");
                                    setTimeout(() => {
                                        copyLinkTarget.classList.remove("copied");
                                        if (copyLinkHint) copyLinkHint.textContent = "Copy URL";
                                    }, 1400);
                                } catch (error) {
                                    await window.modelRadarAlert(
                                        error.message || "Unable to copy the model link.",
                                        {title:"Copy Link", okText:"OK"}
                                    );
                                } finally {
                                    delete copyLinkTarget.dataset.copying;
                                }
                            });
                        }

                        const sourceViewContext = source => {
                            const key = String(source || "combined").trim().toLowerCase();
                            return sourceContexts?.[key] || sourceContexts?.combined || {};
                        };

                        const setDetailText = (selector, value, fallback="Not specified") => {
                            const node = details.querySelector(selector);
                            if (!node) return;
                            const text = String(value ?? "").trim();
                            node.textContent = text || fallback;
                        };

                        const updateDescriptionForSource = context => {
                            const section = details.querySelector("[data-detail-description-section]");
                            const description = details.querySelector("[data-detail-description]");
                            const toggle = details.querySelector(".description-toggle");
                            const text = String(context?.description || "").trim();
                            if (section) section.hidden = !text;
                            if (description) {
                                description.textContent = text;
                                description.classList.toggle("collapsed", text.length > 500);
                            }
                            if (toggle) {
                                toggle.hidden = text.length <= 500;
                                toggle.textContent = "Read more";
                            }
                        };

                        const updateTagsForSource = context => {
                            const list = details.querySelector("[data-detail-tags]");
                            if (!list) return;
                            list.replaceChildren();
                            const tags = Array.isArray(context?.tags) ? context.tags : [];
                            (tags.length ? tags : ["No tags"]).forEach(value => {
                                const tag = document.createElement("span");
                                tag.className = "tag";
                                tag.textContent = String(value || "").trim() || "No tags";
                                list.appendChild(tag);
                            });
                        };

                        const applyDownloadSourceVisibility = source => {
                            const wanted = String(source || "combined").trim().toLowerCase();
                            details.querySelectorAll(".download-source-group[data-source]").forEach(group => {
                                const groupSource = String(group.dataset.source || "").trim().toLowerCase();
                                group.hidden = wanted !== "combined" && groupSource !== wanted;
                            });
                            const chooserHeading = details.querySelector(".download-content > h4");
                            const chooserNote = details.querySelector(".download-content > .download-source-note");
                            if (chooserHeading) chooserHeading.hidden = wanted !== "combined";
                            if (chooserNote) chooserNote.hidden = wanted !== "combined";
                        };

                        const sourceChoiceForVersion = choice => {
                            if (!choice || activeSourceView === "combined") return choice;
                            const sourceMeta = choice?.source_metadata?.[activeSourceView];
                            return sourceMeta ? {...choice, ...sourceMeta, share_url:sourceMeta.share_url || choice.share_url} : choice;
                        };

                        let applySourceView = null;

                        let additionalFilesVisible = false;
                        const setAdditionalFilesVisible = (visible) => {
                            additionalFilesVisible = Boolean(visible);
                            additionalDownloadFiles.forEach(file => {
                                file.hidden = !additionalFilesVisible;
                                if (additionalFilesVisible) {
                                    file.removeAttribute("hidden");
                                    file.style.removeProperty("display");
                                } else {
                                    file.setAttribute("hidden", "");
                                    file.style.setProperty("display", "none", "important");
                                }
                            });
                            additionalFileSections.forEach(section => {
                                section.hidden = !additionalFilesVisible;
                            });
                        };

                        const refreshDownloadFilterBanner = () => {
                            if (!filterBanner) return;
                            const label = filterBanner.querySelector("span");
                            const button = filterBanner.querySelector("button");
                            const hasAdditional = additionalDownloadFiles.length > 0;
                            const hasVersionFilter = Boolean(selectedVersionName);

                            filterBanner.classList.toggle(
                                "open",
                                hasAdditional || hasVersionFilter
                            );

                            if (label) {
                                label.textContent = additionalFilesVisible
                                    ? "Showing additional files"
                                    : "Hiding additional files";
                            }

                            if (button) {
                                button.textContent = additionalFilesVisible
                                    ? "Hide additional files"
                                    : "Show additional files";
                            }
                        };

                        const clearVersionFilter = (showAdditional=false) => {
                            versionGroups.forEach(group => group.classList.remove("version-filter-hidden"));
                            setAdditionalFilesVisible(showAdditional);
                            refreshDownloadFilterBanner();
                        };

                        const syncDownloadSourceAccessForVersion = (name) => {
                            const wanted = String(name || "").trim().toLocaleLowerCase();
                            if (!wanted) return;

                            details.querySelectorAll(".download-source-group").forEach(sourceGroup => {
                                const matchingVersion = Array.from(
                                    sourceGroup.querySelectorAll(".download-version-group[data-version-name]")
                                ).find(group =>
                                    String(group.dataset.versionName || "").trim().toLocaleLowerCase() === wanted
                                );
                                if (!matchingVersion) return;

                                const access = String(matchingVersion.dataset.accessStatus || "").trim().toLowerCase();
                                const badge = sourceGroup.querySelector(".download-source-heading .download-source-state");
                                const paidNote = sourceGroup.querySelector(".download-source-note.paid-access-note");

                                if (badge) {
                                    badge.classList.remove(
                                        "downloadable", "early-access", "paid-access", "gated", "unknown"
                                    );

                                    if (access === "downloadable") {
                                        badge.classList.add("downloadable");
                                        badge.textContent = "↓ Downloadable";
                                    } else if (access === "early_access") {
                                        badge.classList.add("early-access");
                                        badge.textContent = "⚡ Early Access";
                                    } else if (access === "paid_access") {
                                        badge.classList.add("paid-access");
                                        badge.textContent = "$ Paid Access";
                                    } else if (access === "gated") {
                                        badge.classList.add("gated");
                                        badge.textContent = "🔒 Restricted";
                                    } else if (access === "unconfirmed") {
                                        badge.classList.add("unknown");
                                        badge.textContent = "? Unknown";
                                    }
                                }

                                // Only show the paid explanation when the selected version is paid.
                                if (paidNote) {
                                    paidNote.hidden = access !== "paid_access";
                                }
                            });
                        };

                        const filterDownloadsForVersion = (name) => {
                            const wanted = String(name || "").trim().toLocaleLowerCase();
                            // Returning to a selected version always restores the useful,
                            // compact download list.
                            setAdditionalFilesVisible(false);
                            if (!wanted) { clearVersionFilter(false); return 0; }
                            let shown = 0;
                            versionGroups.forEach(group => {
                                const current = String(group.dataset.versionName || "").trim().toLocaleLowerCase();
                                const match = current === wanted;
                                group.classList.toggle("version-filter-hidden", !match);
                                if (match) shown += 1;
                            });
                            syncDownloadSourceAccessForVersion(name);
                            refreshDownloadFilterBanner();
                            return shown;
                        };

                        reloadModelButtons.forEach(button=>{
                            button.addEventListener("click", async(event)=>{
                                event.preventDefault();
                                event.stopPropagation();
                                if(button.dataset.refreshing==="true") return;

                                const modelId=details.querySelector(".model-detail")?.dataset.modelId || "";
                                if(!modelId) return;

                                const original=button.textContent;
                                const originalTitle=button.title;
                                button.dataset.refreshing="true";
                                button.textContent="Reloading";
                                button.title="Reloading model…";
                                button.disabled=true;

                                try{
                                    const response=await fetch(
                                        `/api/model/${encodeURIComponent(modelId)}/refresh-download-sources`,
                                        {
                                            method:"POST",
                                            headers:{"Accept":"application/json"}
                                        }
                                    );
                                    const raw=await response.text();
                                    let data={};
                                    try{data=raw?JSON.parse(raw):{};}catch(_){}

                                    if(!response.ok || !data.success){
                                        const message=
                                            data.error ||
                                            raw ||
                                            `Reload failed (HTTP ${response.status})`;
                                        await window.modelRadarAlert(message,{
                                            title:data.restricted===true
                                                ?"Access required"
                                                :"Reload failed",
                                            okText:"OK",
                                            linkUrl:data.source_url || "",
                                            linkText:data.action_label || (
                                                data.source_label
                                                    ?`Open on ${data.source_label}`
                                                    :""
                                            )
                                        });
                                        return;
                                    }

                                    showModelRadarToast(data.message || "Model reloaded");
                                    window.modelRadarShowWatchNotifications?.();

                                    // Reload can change repository classification (for example a
                                    // Hugging Face LoRA becoming a Collection). Keep the live feed
                                    // card in sync immediately so the user never has to close the
                                    // modal, hard-refresh the browser, and click the card again.
                                    const liveCard=document.querySelector(`.model-card[data-id="${modelId}"]`);
                                    const refreshedCard=(data.card && typeof data.card==="object")?data.card:{};
                                    const refreshedType=String(refreshedCard.model_type||"").trim();
                                    const refreshedName=String(refreshedCard.display_name||"").trim();
                                    if(liveCard){
                                        if(refreshedType){
                                            liveCard.dataset.type=refreshedType.toLowerCase();
                                            const typeBadge=liveCard.querySelector(".badge.type");
                                            if(typeBadge) typeBadge.textContent=refreshedType;
                                        }
                                        if(refreshedName){
                                            liveCard.dataset.name=refreshedName.toLowerCase();
                                            const title=liveCard.querySelector(".card-title h2");
                                            if(title){ title.textContent=refreshedName; title.title=refreshedName; }
                                        }
                                        if(refreshedCard.has_media!==undefined){
                                            liveCard.dataset.hasMedia=refreshedCard.has_media?"true":"false";
                                        }
                                    }

                                    if(data.is_collection && data.collection_url){
                                        closeModelOverlay();
                                        setTimeout(()=>window.location.assign(data.collection_url),120);
                                        return;
                                    }

                                    // Normal models keep the existing behavior: re-open the exact
                                    // model so the Download drawer is rebuilt from the new snapshot.
                                    closeModelOverlay();
                                    setTimeout(()=>{
                                        window.modelRadarOpenModel?.(modelId,{downloads:true});
                                    },180);
                                }catch(error){
                                    await window.modelRadarAlert(
                                        error.message || "Unable to reload model.",
                                        {title:"Reload failed",okText:"OK"}
                                    );
                                }finally{
                                    delete button.dataset.refreshing;
                                    button.disabled=false;
                                    button.textContent=original;
                                    button.title=originalTitle || "Reload this model";
                                }
                            });
                        });

                        const renderVersionSummary = (choice) => {
                            if (!versionSummary) return;
                            versionSummary.innerHTML = "";
                            if (!choice) return;
                            const add = (text, className="") => {
                                if (!text) return;
                                const span = document.createElement("span");
                                span.textContent = text;
                                if (className) span.className = className;
                                versionSummary.appendChild(span);
                            };
                            // Architecture is already visible on the card and access state is
                            // already visible on the version pill. Keep this sticky summary compact.
                            if (choice.base_model_type) add(choice.base_model_type);
                            if (Array.isArray(choice.formats) && choice.formats.length) add(choice.formats.join(" · "));
                        };

                        const selectVersion = (pill) => {
                            if (!pill) return;
                            selectedVersionName = String(pill.dataset.versionName || "").trim();
                            selectedVersionId = String(pill.dataset.versionId || "").trim();
                            versionPills.forEach(node => node.classList.toggle("selected", node === pill));
                            const baseChoice = versionChoices.find(item =>
                                (selectedVersionId && String(item.id || "") === selectedVersionId) ||
                                String(item.name || "").trim().toLocaleLowerCase() === selectedVersionName.toLocaleLowerCase()
                            );
                            const choice = sourceChoiceForVersion(baseChoice);
                            renderVersionSummary(choice);
                            setCopyLinkTarget(baseChoice);
                            if (detailArchitectureBadge) {
                                detailArchitectureBadge.textContent = String(
                                    choice?.architecture || choice?.base_model || defaultDetailArchitecture
                                ).trim() || defaultDetailArchitecture;
                            }
                            syncDownloadSourceAccessForVersion(selectedVersionName);

                            // If Downloads is already open, switching versions should
                            // immediately switch the drawer too. No close/reopen cycle.
                            if (downloadOverlay && downloadOverlay.classList.contains("open")) {
                                filterDownloadsForVersion(selectedVersionName);
                                requestAnimationFrame(resetDownloadScroll);
                            }

                            // gallery.js listens on the inner .model-detail element.
                            // `details` here is the outer #modelDetails wrapper; custom
                            // events bubble upward, never downward, so dispatching on the
                            // wrapper changed the pill highlight but the gallery never
                            // received the version switch.
                            const modelDetail = details.querySelector(".model-detail");
                            (modelDetail || details).dispatchEvent(new CustomEvent("modelradar:version", {
                                detail: {name: selectedVersionName, id: selectedVersionId},
                                bubbles: true
                            }));
                        };

                        applySourceView = (requestedSource, {preserveVersion=true}={}) => {
                            const requested = String(requestedSource || "combined").trim().toLowerCase();
                            activeSourceView = sourceContexts?.[requested] ? requested : "combined";
                            if (sourceViewSelect && sourceViewSelect.value !== activeSourceView) {
                                sourceViewSelect.value = activeSourceView;
                            }

                            const context = sourceViewContext(activeSourceView);
                            const modelDetail = details.querySelector(".model-detail");
                            const detailPanel = overlay.querySelector(".model-panel");
                            const effectiveSource = activeSourceView === "combined"
                                ? String(context?.source || modelDetail?.dataset.source || "").trim().toLowerCase()
                                : activeSourceView;
                            const sourceColor = String(context?.color || "#00eaff").trim() || "#00eaff";

                            if (modelDetail) {
                                modelDetail.dataset.source = effectiveSource;
                                modelDetail.style.setProperty("--source-color", sourceColor);
                            }
                            if (detailPanel) {
                                detailPanel.dataset.source = effectiveSource;
                                detailPanel.style.setProperty("--detail-source-color", sourceColor);
                            }

                            const titleText = details.querySelector(".copy-model-link-title-text");
                            if (titleText) titleText.textContent = String(context?.name || "").trim();
                            const downloadName = details.querySelector(".download-model-name");
                            if (downloadName) downloadName.textContent = String(context?.name || "").trim();

                            details.querySelectorAll(".source-btn[data-source]").forEach(button => {
                                const source = String(button.dataset.source || "").trim().toLowerCase();
                                button.hidden = activeSourceView !== "combined" && source !== activeSourceView;
                            });
                            details.querySelectorAll(".source-author-badge[data-source]").forEach(badge => {
                                const source = String(badge.dataset.source || "").trim().toLowerCase();
                                badge.hidden = activeSourceView !== "combined" && source !== activeSourceView;
                            });
                            applyDownloadSourceVisibility(activeSourceView);

                            setDetailText('[data-detail-field="architecture"]', context?.architecture, defaultDetailArchitecture || "Not specified");
                            setDetailText('[data-detail-field="model_type"]', context?.model_type);
                            setDetailText('[data-detail-field="base_model"]', context?.base_model);
                            setDetailText('[data-detail-field="pipeline"]', context?.pipeline);
                            setDetailText('[data-detail-field="format"]', context?.format);
                            setDetailText('[data-detail-field="license"]', context?.license);
                            setDetailText('[data-detail-field="parameters"]', context?.parameters);
                            setDetailText('[data-detail-field="quantization"]', context?.quantization);
                            setDetailText('[data-detail-stat="downloads"]', context?.downloads, "0");
                            setDetailText('[data-detail-stat="likes"]', context?.likes, "0");
                            setDetailText('[data-detail-stat="created"]', context?.created, "Not specified");
                            setDetailText('[data-detail-stat="updated"]', context?.updated, "Not specified");
                            updateDescriptionForSource(context);
                            updateTagsForSource(context);

                            const fullNameSection = details.querySelector("[data-detail-full-name-section]");
                            const fullName = details.querySelector("[data-detail-full-name]");
                            const currentName = String(context?.name || "").trim();
                            if (fullName) fullName.textContent = currentName;
                            if (fullNameSection) fullNameSection.hidden = currentName.length <= 90;

                            versionPills.forEach(pill => {
                                const sources = String(pill.dataset.versionSources || "")
                                    .trim().toLowerCase().split(/\s+/).filter(Boolean);
                                pill.hidden = activeSourceView !== "combined" && !sources.includes(activeSourceView);
                            });

                            const visiblePills = Array.from(versionPills).filter(pill => !pill.hidden);
                            let selectedPill = Array.from(versionPills).find(pill => pill.classList.contains("selected") && !pill.hidden);
                            if (!preserveVersion || !selectedPill) selectedPill = visiblePills[0] || null;

                            (modelDetail || details).dispatchEvent(new CustomEvent("modelradar:source", {
                                detail: {source: activeSourceView},
                                bubbles: true
                            }));

                            if (selectedPill) selectVersion(selectedPill);
                            else {
                                selectedVersionName = "";
                                selectedVersionId = "";
                                setCopyLinkTarget(null);
                            }
                            requestAnimationFrame(updateVersionScrollControls);
                        };

                        if (sourceViewSelect) {
                            sourceViewSelect.addEventListener("change", event => {
                                event.stopPropagation();
                                applySourceView(event.target.value);
                            });
                        }

                        if (downloadContent && additionalDownloadFiles.length > 0) {
                            filterBanner = document.createElement("div");
                            filterBanner.className = "download-filter-banner";
                            filterBanner.innerHTML = `<span>Hiding additional files</span><button type="button">Show additional files</button>`;

                            // Keep the toggle anchored between the useful model
                            // files and support/additional files. It must never
                            // travel to the bottom when a repository contains
                            // dozens of support files.
                            const firstExtraSection = details.querySelector(".download-extra-files-section");
                            if (firstExtraSection) {
                                firstExtraSection.before(filterBanner);
                            } else {
                                downloadContent.append(filterBanner);
                            }
                            filterBanner.querySelector("button").addEventListener("click", (event) => {
                                event.stopPropagation();

                                if (!additionalFilesVisible) {
                                    setAdditionalFilesVisible(true);
                                } else {
                                    setAdditionalFilesVisible(false);
                                }

                                refreshDownloadFilterBanner();
                                requestAnimationFrame(resetDownloadScroll);
                            });

                            // Providers without explicit version pills still need a visible
                            // Show all control whenever support files are hidden.
                            refreshDownloadFilterBanner();
                        }

                        versionPills.forEach(pill => {
                            pill.addEventListener("click", (event) => {
                                event.stopPropagation();
                                selectVersion(pill);
                            });
                        });

                        if (versionDownload && downloadOverlay) {
                            versionDownload.addEventListener("click", event => {
                                event.stopPropagation();
                                filterDownloadsForVersion(selectedVersionName);
                                const isOpen = downloadOverlay.classList.toggle("open");
                                versionDownload.classList.toggle("active", isOpen);
                                versionDownload.textContent = isOpen ? "Hide Downloads" : "Show Downloads";
                                setReloadModelVisible(isOpen);
                                if (isOpen) requestAnimationFrame(resetDownloadScroll);
                            });
                        }

                        // In local-install mode the same Download arrow is used, but
                        // AbyssBeacon downloads server-side into the configured library instead
                        // of opening the browser's save dialog.
                        details.querySelectorAll("a.file-download-btn").forEach(link => {
                            link.addEventListener("click", async event => {
                                const prefs = window.userPreferences || {};
                                if (String(prefs.download_behavior || "browser").toLowerCase() !== "local") return;
                                event.preventDefault();
                                event.stopPropagation();
                                if (link.dataset.installing === "true") return;
                                const original = link.textContent;
                                link.dataset.installing = "true";
                                link.textContent = "…";
                                link.title = "Installing to local AbyssBeacon library…";
                                try {
                                    const installUrl = new URL(link.href, window.location.href);
                                    installUrl.searchParams.set("save_info", saveInfoForDownload?.checked ? "1" : "0");
                                    installUrl.searchParams.set("save_preview", savePreviewForDownload?.checked ? "1" : "0");
                                    const response = await fetch(installUrl.toString(), {headers:{"Accept":"application/json"}});
                                    const raw = await response.text();
                                    let data = {};
                                    try { data = raw ? JSON.parse(raw) : {}; } catch (_) {}
                                    if (!response.ok || !data.success) {
                                        const failure = new Error(
                                            data.message ||
                                            data.error ||
                                            raw ||
                                            `Install failed (HTTP ${response.status})`
                                        );
                                        failure.payload = data;
                                        failure.httpStatus = response.status;
                                        throw failure;
                                    }

                                    // A saved partial or already-running download is not a
                                    // completed install. Open Download Manager instead of
                                    // starting/resuming another writer against the same .part.
                                    if (data.existing_partial === true || data.already_active === true) {
                                        link.textContent = original;
                                        link.title = data.existing_partial === true
                                            ? "Partial download saved — resume in Download Manager"
                                            : "This file is already downloading";
                                        document.getElementById("downloadHistoryButton")?.click();
                                        return;
                                    }

                                    link.textContent = "✓";
                                    link.title = `Installed: ${data.path || data.folder || "local library"}`;
                                    const row = link.closest(".download-file");
                                    const info = row?.querySelector(".download-file-info span");
                                    if (info && data.path) info.textContent = `Installed → ${data.path}`;
                                    const modelName =
                                        details.querySelector(".download-model-name")?.textContent?.trim() ||
                                        details.querySelector(".model-detail-title")?.textContent?.trim() ||
                                        "model";

                                    // The server has already recorded download history.
                                    // Update the visible feed/creator card immediately
                                    // rather than requiring a browser refresh.
                                    markModelRadarCardDownloaded(details.dataset.modelId);

                                    showModelRadarToast(`Successfully downloaded "${modelName}"`);
                                } catch (error) {
                                    link.textContent = "!";
                                    link.title = error.message || "Local install failed";
                                    const message = error.message || "Local install failed";
                                    const payload = error.payload || {};

                                    // If the browser loses its connection to the local Flask
                                    // server (for example AbyssBeacon is closed/restarted), fetch()
                                    // rejects before an HTTP response exists. The downloader keeps
                                    // the .part file on disk and restores the job on next startup,
                                    // so this is an expected interruption rather than a download
                                    // failure. Keep genuine HTTP/source failures visible below.
                                    const isLocalConnectionLoss =
                                        !error.httpStatus &&
                                        Object.keys(payload).length === 0 &&
                                        (error instanceof TypeError ||
                                         /networkerror|failed to fetch|network request failed|load failed/i.test(message));

                                    if (isLocalConnectionLoss) {
                                        link.textContent = original;
                                        link.title = "Download paused — partial file preserved";
                                        return;
                                    }

                                    // Cancel is an expected user action. Active Downloads
                                    // already shows Canceling → Canceled and confirms that
                                    // the partial file was removed, so do not turn it into a
                                    // second red "Download failed" dialog.
                                    if (payload.canceled === true) {
                                        link.textContent = original;
                                        link.title = "Download canceled";
                                        return;
                                    }

                                    const sourceGroup = link.closest(".download-source-group");
                                    const versionGroup = link.closest(".download-version-group");
                                    const sourceAccess =
                                        String(sourceGroup?.dataset.accessStatus || "").toLowerCase();
                                    const versionAccess =
                                        String(versionGroup?.dataset.accessStatus || "").toLowerCase();
                                    const sourceLabel =
                                        sourceGroup?.dataset.sourceLabel ||
                                        payload.source_label ||
                                        "source";
                                    const sourceUrl =
                                        payload.source_url ||
                                        sourceGroup?.dataset.sourceUrl ||
                                        "";
                                    const isRestricted =
                                        payload.restricted === true ||
                                        sourceAccess === "gated" ||
                                        versionAccess === "gated";
                                    const isSeaArtAuth =
                                        !isRestricted &&
                                        /seaart/i.test(message) &&
                                        /auth|token|signed in/i.test(message);

                                    const popupMessage = isRestricted
                                        ? (
                                            `This model is restricted on ${sourceLabel}. ` +
                                            `Your current account does not have download access, ` +
                                            `or the source could not verify that access. Open the ` +
                                            `model on ${sourceLabel} to request, purchase, accept, ` +
                                            `or otherwise unlock access, then try again.`
                                        )
                                        : message;

                                    await window.modelRadarAlert(popupMessage, {
                                        title: isRestricted
                                            ? "Access required"
                                            : (isSeaArtAuth
                                                ? "SeaArt authentication expired"
                                                : "Download failed"),
                                        okText: "OK",
                                        linkUrl: isRestricted ? sourceUrl : "",
                                        linkText: isRestricted
                                            ? (payload.action_label || `Open on ${sourceLabel}`)
                                            : ""
                                    });
                                    setTimeout(()=>{link.textContent=original;},1600);
                                } finally {
                                    delete link.dataset.installing;
                                }
                            });
                        });

                        details.querySelectorAll(".download-when-available").forEach(button=>{
                            button.addEventListener("click",async event=>{
                                event.preventDefault();
                                event.stopPropagation();
                                if(button.dataset.queued==="true") return;
                                const original=button.textContent;
                                button.disabled=true;
                                button.textContent="Adding…";
                                try{
                                    const response=await fetch("/api/download-queue",{
                                        method:"POST",
                                        headers:{"Content-Type":"application/json"},
                                        body:JSON.stringify({
                                            model_id:Number(button.dataset.modelId||0),
                                            source:button.dataset.source||"",
                                            version_id:button.dataset.versionId||"",
                                            version_name:button.dataset.versionName||"",
                                            release_at:button.dataset.releaseAt||"",
                                        }),
                                    });
                                    const data=await response.json();
                                    if(!response.ok||!data.success)throw new Error(data.error||"Unable to add this release to the queue.");
                                    button.dataset.queued="true";
                                    button.textContent="✓ Waiting in queue";
                                    button.classList.add("queued");
                                    showModelRadarToast(`Queued "${button.dataset.versionName||"this version"}" for download when available`);
                                }catch(error){
                                    button.disabled=false;
                                    button.textContent=original;
                                    await window.modelRadarAlert(error.message||"Unable to add to download queue",{title:"Queue failed"});
                                }
                            });
                        });

                        details.querySelectorAll(".file-watch-btn").forEach(button=>{
                            button.addEventListener("click",async event=>{
                                event.preventDefault();
                                event.stopPropagation();
                                if(button.dataset.watching==="true") return;
                                const original=button.textContent;
                                button.disabled=true;
                                button.textContent="Adding…";
                                try{
                                    const response=await fetch("/api/download-watchlist",{
                                        method:"POST",
                                        headers:{"Content-Type":"application/json"},
                                        body:JSON.stringify({
                                            model_id:Number(button.dataset.modelId||0),
                                            source:button.dataset.source||"",
                                            version_id:button.dataset.versionId||"",
                                            version_name:button.dataset.versionName||"",
                                            file_id:button.dataset.fileId||"",
                                            file_name:button.dataset.fileName||"",
                                            file_index:Number(button.dataset.fileIndex||-1),
                                        }),
                                    });
                                    const data=await response.json();
                                    if(!response.ok||!data.success)throw new Error(data.error||"Unable to watch this paid file.");
                                    button.dataset.watching="true";
                                    button.textContent="✓ Watching";
                                    button.classList.add("watching");
                                    button.disabled=false;
                                    showModelRadarToast(`Watching "${button.dataset.fileName||"this file"}" for availability`);
                                }catch(error){
                                    button.disabled=false;
                                    button.textContent=original;
                                    await window.modelRadarAlert(error.message||"Unable to add file to Watchlist",{title:"Watchlist failed"});
                                }
                            });
                        });

                        setAdditionalFilesVisible(false);
                        // Combined is the transparent default even though the
                        // source-aware viewer selector is intentionally not exposed.
                        // This keeps all eligible provider snapshots merged according
                        // to the user's Mature Content setting.
                        if (applySourceView) applySourceView("combined", {preserveVersion:false});
                        else if (versionPills.length) selectVersion(versionPills[0]);
                        else setCopyLinkTarget(null);


                        if (
                            showAllDownloads &&
                            allDownloads
                        ) {

                            showAllDownloads.addEventListener(
                                "click",
                                (event) => {

                                    event.stopPropagation();

                                    const isOpen =
                                        allDownloads.classList.toggle(
                                            "open"
                                        );


                                    showAllDownloads.textContent =
                                        isOpen
                                            ? "Hide additional files"
                                            : "Show all downloads";

                                }
                            );

                        }

                        const closeButton = details.querySelector(".close-model");

                        if (closeButton) {

                            closeButton.addEventListener("click", () => {

                                closeModelOverlay();

                            });

                        }

                        // FLOATING MODEL NAVIGATION
                        // The main header intentionally scrolls away. Once the user has
                        // moved into the card, keep only the two actions that matter:
                        // return to top and close.
                        const floatingNav = details.querySelector(".detail-floating-nav");
                        const backToTop = details.querySelector(".detail-back-to-top");
                        const floatingClose = details.querySelector(".detail-floating-close");

                        const syncFloatingNav = () => {
                            if (!floatingNav) return;
                            floatingNav.classList.toggle("visible", details.scrollTop > 280);
                        };

                        if (backToTop) {
                            backToTop.addEventListener("click", (event) => {
                                event.preventDefault();
                                event.stopPropagation();
                                details.scrollTo({top: 0, behavior: "smooth"});
                            });
                        }

                        if (floatingClose) {
                            floatingClose.addEventListener("click", (event) => {
                                event.preventDefault();
                                event.stopPropagation();
                                closeModelOverlay();
                            });
                        }

                        details.addEventListener("scroll", syncFloatingNav, {passive: true});
                        syncFloatingNav();


                        activeGalleryCleanup = initializeGallery() || null;


                        overlay.classList.add(
                            "open"
                        );


                        // Every card opens from the top, regardless of where the
                        // previously viewed card was scrolled.
                        const panel =
                            document.querySelector(".model-panel");

                        if (panel) {
                            panel.scrollTop = 0;
                        }
                        details.scrollTop = 0;

                        // Run once after layout as well; this avoids the browser restoring
                        // the previous scroll position while the fetched detail HTML paints.
                        requestAnimationFrame(() => {
                            details.scrollTop = 0;
                            if (panel) panel.scrollTop = 0;
                        });

                        document.body.style.overflow = "hidden";

                        // The cyan ↓ on a feed card is a download shortcut:
                        // open the model normally, but enter directly into its
                        // download workflow rather than requiring a second click.
                        if(openDownloadsFromCard && versionDownload && downloadOverlay){
                            filterDownloadsForVersion(selectedVersionName);
                            downloadOverlay.classList.add("open");
                            versionDownload.classList.add("active");
                            versionDownload.textContent = "Hide Downloads";
                            setReloadModelVisible(true);
                            requestAnimationFrame(resetDownloadScroll);
                        }


                    });


    });



    overlay.addEventListener("click",(event)=>{
        if(event.target===overlay) closeModelOverlay();
    });

    document.addEventListener("keydown",(event)=>{
        if(event.key!=="Escape" || !overlay.classList.contains("open")) return;
        const imageOverlay=document.getElementById("imageOverlay");
        if(imageOverlay?.classList.contains("open")) return;
        closeModelOverlay();
    });


}