// Full-page Collection views explicitly request restoration of the feed's last
// saved scroll position. This is more reliable than browser history alone and
// still lets an intentional browser refresh of the home page start at the top.
document.addEventListener("DOMContentLoaded", () => {
    const back = document.querySelector(".collection-back");
    back?.addEventListener("click", event => {
        event.preventDefault();
        try{ sessionStorage.setItem("abyss_feed_restore_pending_v1","1"); }catch(_){ }
        window.location.assign("/");
    });

    const copyButton = document.getElementById("collectionCopyLinkButton");
    copyButton?.addEventListener("click", async () => {
        if(copyButton.dataset.copying === "true") return;
        const value = String(copyButton.dataset.copyUrl || "").trim();
        if(!value) return;
        const original = copyButton.textContent;
        copyButton.dataset.copying = "true";
        try{
            if(navigator.clipboard?.writeText && window.isSecureContext){
                await navigator.clipboard.writeText(value);
            }else{
                const textarea = document.createElement("textarea");
                textarea.value = value;
                textarea.setAttribute("readonly", "");
                textarea.style.position = "fixed";
                textarea.style.opacity = "0";
                textarea.style.pointerEvents = "none";
                document.body.appendChild(textarea);
                textarea.select();
                textarea.setSelectionRange(0, textarea.value.length);
                const copied = document.execCommand("copy");
                textarea.remove();
                if(!copied) throw new Error("Clipboard copy was blocked by the browser.");
            }
            copyButton.textContent = "Copied!";
            copyButton.classList.add("copied");
            window.setTimeout(() => {
                copyButton.textContent = original;
                copyButton.classList.remove("copied");
            }, 1400);
        }catch(error){
            console.error("Collection link copy failed:", error);
            copyButton.textContent = "Copy failed";
            window.setTimeout(() => { copyButton.textContent = original; }, 1600);
        }finally{
            delete copyButton.dataset.copying;
        }
    });
});

document.addEventListener("DOMContentLoaded", () => {
    const search = document.getElementById("collectionFamilySearch");
    const count = document.getElementById("collectionSearchCount");
    const empty = document.getElementById("collectionSearchEmpty");
    const expandAll = document.getElementById("collectionExpandAll");
    const cards = Array.from(document.querySelectorAll(".collection-child-card"));

    function visibleCards(){
        return cards.filter(card => !card.hidden);
    }

    function updateExpandAllState(){
        if(!expandAll) return;
        const visible = visibleCards();
        const allOpen = visible.length > 0 && visible.every(card => card.open);
        expandAll.setAttribute("aria-pressed", allOpen ? "true" : "false");
        expandAll.textContent = allOpen ? "Collapse all" : "Expand all";
    }

    function updateSearch(){
        if(!search) return;
        const query = String(search.value || "").trim().toLowerCase();
        let shown = 0;
        for(const card of cards){
            const haystack = String(card.dataset.familySearch || "").toLowerCase();
            const visible = !query || haystack.includes(query);
            card.hidden = !visible;
            if(visible) shown += 1;
        }
        if(count) count.textContent = `${shown} shown`;
        if(empty) empty.hidden = shown !== 0;
        updateExpandAllState();
    }

    if(search){
        search.addEventListener("input", updateSearch);
    }

    expandAll?.addEventListener("click", () => {
        const visible = visibleCards();
        const shouldOpen = !(visible.length > 0 && visible.every(card => card.open));
        visible.forEach(card => { card.open = shouldOpen; });
        updateExpandAllState();
    });

    cards.forEach(card => card.addEventListener("toggle", updateExpandAllState));
    updateExpandAllState();

    document.querySelectorAll(".collection-family-favorite").forEach(button => {
        button.addEventListener("click", async event => {
            event.preventDefault();
            event.stopPropagation();
            if(button.disabled) return;

            const current = button.getAttribute("aria-pressed") === "true";
            const desired = !current;
            button.disabled = true;
            try{
                const response = await fetch(button.dataset.favoriteUrl, {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({favorite: desired}),
                });
                const payload = await response.json().catch(() => ({}));
                if(!response.ok || payload.success !== true){
                    throw new Error(payload.error || "Could not update favorite");
                }
                button.setAttribute("aria-pressed", desired ? "true" : "false");
                button.classList.toggle("is-favorite", desired);
                button.textContent = desired ? "★" : "☆";
                button.title = desired ? "Remove family from favorites" : "Favorite this family";
            }catch(error){
                console.error("Collection family favorite failed:", error);
                button.title = "Could not update favorite";
            }finally{
                button.disabled = false;
            }
        });

        // Clicking the star should never toggle the surrounding <details> card.
        button.addEventListener("mousedown", event => event.stopPropagation());
    });
});

// Collection pages are full-page views, so their reload control can use the
// same source-aware backend reload as normal model detail and then rebuild the
// page from the freshly stored repository snapshot.
document.addEventListener("DOMContentLoaded", () => {
    const button = document.getElementById("collectionReloadButton");
    const status = document.getElementById("collectionReloadStatus");
    if(!button) return;

    button.addEventListener("click", async () => {
        if(button.dataset.refreshing === "true") return;
        const reloadUrl = String(button.dataset.reloadUrl || "").trim();
        if(!reloadUrl) return;

        const originalText = button.textContent;
        button.dataset.refreshing = "true";
        button.disabled = true;
        button.textContent = "Reloading…";
        if(status) status.textContent = "Refreshing repository metadata, files, and media…";

        try{
            const response = await fetch(reloadUrl, {
                method: "POST",
                headers: {"Accept": "application/json"},
            });
            const raw = await response.text();
            let payload = {};
            try{ payload = raw ? JSON.parse(raw) : {}; }catch(_){ }

            if(!response.ok || payload.success !== true){
                throw new Error(payload.error || payload.message || raw || `Reload failed (HTTP ${response.status})`);
            }

            if(status) status.textContent = payload.message || "Collection reloaded.";
            // A real navigation (rather than DOM patching) guarantees title,
            // family grouping, favorites, and preview media all come from the
            // same newly persisted source snapshot.
            window.setTimeout(() => window.location.reload(), 220);
        }catch(error){
            console.error("Collection reload failed:", error);
            if(status) status.textContent = error.message || "Unable to reload Collection.";
            button.disabled = false;
            button.textContent = originalText;
            delete button.dataset.refreshing;
        }
    });
});

// Collection media deliberately reuses the same gallery.js/fullscreen.js
// implementation as an ordinary model card. The page shows 10 thumbnails at
// a time, while the viewer receives the entire repository media list.
document.addEventListener("DOMContentLoaded", () => {
    const overlay = document.getElementById("collectionMediaOverlay");
    const detail = overlay?.querySelector(".collection-media-detail");
    const close = document.getElementById("collectionMediaClose");
    const strip = document.getElementById("collectionMediaStrip");
    const pagePrev = document.getElementById("collectionMediaPagePrev");
    const pageNext = document.getElementById("collectionMediaPageNext");
    const pageStatus = document.getElementById("collectionMediaPageStatus");
    const viewerModelButton = document.getElementById("collectionMediaModelButton");
    if(!overlay || !detail) return;

    const collectionRestricted = String(document.body.dataset.accessStatus || "").toLowerCase() === "gated";

    function markPreviewUnavailable(image){
        if(!image) return;
        const host = image.closest(".collection-media-item, .collection-file-preview");
        if(!host || host.classList.contains("is-unavailable")) return;
        host.classList.add("is-unavailable");
        host.dataset.restricted = collectionRestricted ? "true" : "false";
        image.hidden = true;

        let fallback = host.querySelector(".collection-media-fallback, .collection-file-preview-fallback");
        if(!fallback){
            fallback = document.createElement("span");
            fallback.className = host.classList.contains("collection-media-item")
                ? "collection-media-fallback"
                : "collection-file-preview-fallback";
            host.appendChild(fallback);
        }
        fallback.textContent = collectionRestricted ? "🔒 Preview restricted" : "Preview unavailable";
        fallback.setAttribute("aria-hidden", "false");

        // Do not open the media viewer on a URL the browser has already proved
        // it cannot display. A sibling View model control, when present, stays
        // active so the user can still jump directly to the model file.
        host.disabled = true;
        host.title = collectionRestricted
            ? "Preview is restricted by the source"
            : "Preview is unavailable";
    }

    function bindPreviewFallback(image){
        if(!image || image.dataset.collectionFallbackBound === "true") return;
        image.dataset.collectionFallbackBound = "true";
        image.addEventListener("error", () => markPreviewUnavailable(image), {once: true});
        if(image.complete && image.naturalWidth === 0) markPreviewUnavailable(image);
    }

    let mediaData = [];
    try{
        mediaData = JSON.parse(detail.querySelector(".media-data-json")?.textContent || "[]") || [];
    }catch(_){
        mediaData = [];
    }
    if(!mediaData.length) return;

    const PAGE_SIZE = 10;
    let mediaPage = 0;

    function mediaItem(index){
        return mediaData[index] || {};
    }

    function closeViewer(){
        overlay.classList.remove("open");
        overlay.setAttribute("aria-hidden", "true");
        document.body.classList.remove("collection-media-open");
        detail.querySelectorAll("video").forEach(video => {
            try{ video.pause(); }catch(_){ }
        });
    }

    function openViewer(index){
        const safeIndex = Math.max(0, Math.min(mediaData.length - 1, Number.parseInt(index, 10) || 0));
        overlay.classList.add("open");
        overlay.setAttribute("aria-hidden", "false");
        document.body.classList.add("collection-media-open");
        detail.dispatchEvent(new CustomEvent("modelradar:gallery-show", {
            detail: {index: safeIndex},
        }));
    }

    function hasModelMatch(item){
        return !!String(item?._collection_file_dom_id || "").trim();
    }

    function focusMatchedFile(index){
        const item = mediaItem(index);
        const familyDomId = String(item._collection_family_dom_id || "").trim();
        const fileDomId = String(item._collection_file_dom_id || "").trim();
        if(!familyDomId || !fileDomId) return;

        const search = document.getElementById("collectionFamilySearch");
        if(search && search.value){
            search.value = "";
            search.dispatchEvent(new Event("input", {bubbles: true}));
        }

        const family = document.getElementById(familyDomId);
        const row = document.getElementById(fileDomId);
        if(!family || !row) return;

        family.hidden = false;
        family.open = true;
        closeViewer();

        document.querySelectorAll(".collection-file-row.is-focused").forEach(node => node.classList.remove("is-focused"));
        row.classList.add("is-focused");
        try{ history.replaceState(null, "", `#${fileDomId}`); }catch(_){ }

        window.requestAnimationFrame(() => {
            window.requestAnimationFrame(() => row.scrollIntoView({behavior: "smooth", block: "center"}));
        });
        window.setTimeout(() => row.classList.remove("is-focused"), 3200);
    }

    function buildMediaCard(index){
        const item = mediaItem(index);
        const card = document.createElement("div");
        card.className = "collection-media-card";
        card.dataset.mediaIndex = String(index);

        const mediaButton = document.createElement("button");
        mediaButton.type = "button";
        mediaButton.className = "collection-media-item";
        mediaButton.dataset.mediaIndex = String(index);
        mediaButton.title = `Open ${item.filename || `Preview ${index + 1}`} in the AbyssBeacon media viewer`;
        mediaButton.setAttribute("aria-label", `Open ${item.filename || `preview ${index + 1}`} in the media viewer`);

        const image = document.createElement("img");
        image.src = item.thumbnail || item.url || "";
        image.alt = item.filename || `Preview ${index + 1}`;
        image.loading = "lazy";
        image.referrerPolicy = "no-referrer";
        bindPreviewFallback(image);

        const fallback = document.createElement("span");
        fallback.className = "collection-media-fallback";
        fallback.setAttribute("aria-hidden", "true");

        const hint = document.createElement("span");
        hint.className = "collection-media-open-hint";
        hint.textContent = "View media";
        mediaButton.append(image, fallback, hint);
        card.appendChild(mediaButton);

        if(hasModelMatch(item)){
            const modelButton = document.createElement("button");
            modelButton.type = "button";
            modelButton.className = "collection-media-model-link";
            modelButton.dataset.mediaIndex = String(index);
            modelButton.title = `Open ${item._collection_file_name || "the matching model file"}`;
            modelButton.textContent = "View model";
            card.appendChild(modelButton);
        }
        return card;
    }

    function renderMediaPage(){
        if(!strip) return;
        const pageCount = Math.max(1, Math.ceil(mediaData.length / PAGE_SIZE));
        mediaPage = Math.max(0, Math.min(mediaPage, pageCount - 1));
        const start = mediaPage * PAGE_SIZE;
        const end = Math.min(mediaData.length, start + PAGE_SIZE);
        const fragment = document.createDocumentFragment();
        for(let index = start; index < end; index += 1){
            fragment.appendChild(buildMediaCard(index));
        }
        strip.replaceChildren(fragment);
        if(pageStatus) pageStatus.textContent = `Page ${mediaPage + 1} of ${pageCount} · ${mediaData.length} previews`;
        if(pagePrev) pagePrev.disabled = mediaPage <= 0;
        if(pageNext) pageNext.disabled = mediaPage >= pageCount - 1;
    }

    function updateViewerModelButton(index){
        if(!viewerModelButton) return;
        const item = mediaItem(index);
        const matched = hasModelMatch(item);
        viewerModelButton.hidden = !matched;
        viewerModelButton.dataset.mediaIndex = matched ? String(index) : "";
        if(matched){
            viewerModelButton.title = `Open ${item._collection_file_name || "the matching model file"}`;
        }
    }

    // Listen before gallery initialization so every image change, including the
    // first one, can update the Collection-specific View model action.
    detail.addEventListener("modelradar:gallery-change", event => {
        const index = Number.parseInt(event.detail?.index, 10);
        if(Number.isFinite(index)) updateViewerModelButton(index);
    });

    if(typeof initializeGallery === "function") initializeGallery(detail);
    if(typeof initializeFullscreen === "function") initializeFullscreen();

    renderMediaPage();

    strip?.addEventListener("click", event => {
        const modelButton = event.target.closest(".collection-media-model-link[data-media-index]");
        if(modelButton){
            event.preventDefault();
            event.stopPropagation();
            focusMatchedFile(Number.parseInt(modelButton.dataset.mediaIndex || "0", 10));
            return;
        }
        const mediaButton = event.target.closest(".collection-media-item[data-media-index]");
        if(mediaButton){
            openViewer(Number.parseInt(mediaButton.dataset.mediaIndex || "0", 10));
        }
    });

    document.querySelectorAll(".collection-file-preview[data-media-index]").forEach(button => {
        bindPreviewFallback(button.querySelector("img"));
        button.addEventListener("click", event => {
            event.preventDefault();
            event.stopPropagation();
            openViewer(Number.parseInt(button.dataset.mediaIndex || "0", 10));
        });
    });

    viewerModelButton?.addEventListener("click", () => {
        const index = Number.parseInt(viewerModelButton.dataset.mediaIndex || "", 10);
        if(Number.isFinite(index)) focusMatchedFile(index);
    });

    pagePrev?.addEventListener("click", () => {
        if(mediaPage <= 0) return;
        mediaPage -= 1;
        renderMediaPage();
    });
    pageNext?.addEventListener("click", () => {
        const pageCount = Math.max(1, Math.ceil(mediaData.length / PAGE_SIZE));
        if(mediaPage >= pageCount - 1) return;
        mediaPage += 1;
        renderMediaPage();
    });

    close?.addEventListener("click", closeViewer);
    overlay.addEventListener("click", event => {
        if(event.target === overlay) closeViewer();
    });
    document.addEventListener("keydown", event => {
        if(event.key === "Escape" && overlay.classList.contains("open")){
            const fullscreen = document.getElementById("imageOverlay");
            if(fullscreen?.classList.contains("open")) return;
            closeViewer();
        }
    });
});


// Match the main feed's unobtrusive back-to-top control on long Collection pages.
document.addEventListener("DOMContentLoaded", () => {
    const button = document.getElementById("collectionBackToTop");
    if(!button) return;
    const update = () => button.classList.toggle("visible", window.scrollY > 700);
    window.addEventListener("scroll", update, {passive: true});
    button.addEventListener("click", () => window.scrollTo({top: 0, behavior: "smooth"}));
    update();
});
