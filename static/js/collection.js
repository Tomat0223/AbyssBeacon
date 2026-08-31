document.addEventListener("DOMContentLoaded", () => {
    const search = document.getElementById("collectionFamilySearch");
    const count = document.getElementById("collectionSearchCount");
    const empty = document.getElementById("collectionSearchEmpty");
    const cards = Array.from(document.querySelectorAll(".collection-child-card"));

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
    }

    if(search){
        search.addEventListener("input", updateSearch);
    }

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
// implementation as an ordinary model card. The grid remains a lightweight
// set of thumbnails; selecting one opens the familiar viewer with metadata,
// previous/next navigation, expanded mode, and fullscreen zoom/pan.
document.addEventListener("DOMContentLoaded", () => {
    const overlay = document.getElementById("collectionMediaOverlay");
    const detail = overlay?.querySelector(".collection-media-detail");
    const close = document.getElementById("collectionMediaClose");
    const thumbnails = Array.from(document.querySelectorAll(".collection-media-item[data-media-index]"));
    if(!overlay || !detail || !thumbnails.length) return;

    if(typeof initializeGallery === "function") initializeGallery(detail);
    if(typeof initializeFullscreen === "function") initializeFullscreen();

    function openViewer(index){
        overlay.classList.add("open");
        overlay.setAttribute("aria-hidden", "false");
        document.body.classList.add("collection-media-open");
        detail.dispatchEvent(new CustomEvent("modelradar:gallery-show", {
            detail: {index},
        }));
    }

    function closeViewer(){
        overlay.classList.remove("open");
        overlay.setAttribute("aria-hidden", "true");
        document.body.classList.remove("collection-media-open");
        // If a Collection contains video media in the future, hiding the
        // viewer should stop playback just like closing a normal model card.
        detail.querySelectorAll("video").forEach(video => {
            try{ video.pause(); }catch(_){ }
        });
    }

    thumbnails.forEach(button => {
        button.addEventListener("click", () => {
            const index = Number.parseInt(button.dataset.mediaIndex || "0", 10);
            openViewer(Number.isFinite(index) ? index : 0);
        });
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
