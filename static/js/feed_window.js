function initializeFeedWindowing(){
    const feed=document.getElementById("modelFeed");
    const grid=document.getElementById("modelFeedGrid");
    const topSentinel=document.getElementById("feedTopSentinel");
    const bottomSentinel=document.getElementById("feedLoadSentinel");
    if(!feed || !grid || !bottomSentinel) return;

    let loading=false;
    let requestGeneration=0;
    let activeController=null;
    let windowStart=Number(feed.dataset.windowStart || 0) || 0;
    let total=Number(feed.dataset.totalModelCount || 0) || 0;

    const chunkSize=80;
    const replaceSize=120;
    // Keep the mounted Feed deliberately small. The old implementation appended
    // every visited chunk for the entire page lifetime, so images/videos/cards
    // accumulated until scrolling itself became expensive.
    const maxMountedCards=120;
    const feedReturnScrollKey="abyss_feed_return_scroll_v1";
    const feedReturnWindowStartKey="abyss_feed_return_window_start_v1";
    const feedRestorePendingKey="abyss_feed_restore_pending_v1";
    let scrollSaveQueued=false;
    let scrollDirection="down";
    let lastScrollY=Math.max(0,window.scrollY || 0);
    let adjustingScroll=false;
    let feedWindowSuspended=false;

    function isFeedWindowSuspended(){
        return feedWindowSuspended
            || Boolean(document.getElementById("modelOverlay")?.classList.contains("open"));
    }

    function setFeedWindowSuspended(value){
        feedWindowSuspended=Boolean(value);
        window.modelRadarFeedWindowSuspended=feedWindowSuspended;

        if(feedWindowSuspended){
            // A model modal can temporarily change the browser's reported
            // scroll position while the background is locked. Do not let that
            // wake upward/downward paging or finish an in-flight chunk.
            requestGeneration += 1;
            activeController?.abort();
            activeController=null;
            loading=false;
            topSentinel?.classList.remove("loading");
            bottomSentinel?.classList.remove("loading");
            return;
        }

        lastScrollY=Math.max(0,window.scrollY || 0);
        adjustingScroll=false;
        queueFeedReturnPositionSave();
    }

    function saveFeedReturnPosition(){
        if(isFeedWindowSuspended()) return;
        try{
            sessionStorage.setItem(feedReturnScrollKey,String(Math.max(0,Math.round(window.scrollY || 0))));
            sessionStorage.setItem(feedReturnWindowStartKey,String(Math.max(0,windowStart || 0)));
        }catch(_){ }
    }

    function queueFeedReturnPositionSave(){
        if(scrollSaveQueued) return;
        scrollSaveQueued=true;
        requestAnimationFrame(()=>{
            scrollSaveQueued=false;
            saveFeedReturnPosition();
        });
    }

    window.addEventListener("scroll",queueFeedReturnPositionSave,{passive:true});
    window.addEventListener("pagehide",saveFeedReturnPosition);

    function mountedCards(){
        return Array.from(grid.querySelectorAll(":scope > .model-card"));
    }

    function mountedCount(){
        return mountedCards().length;
    }

    function windowEnd(){
        return windowStart + mountedCount();
    }

    function syncFeedState(){
        const count=mountedCount();
        const end=windowStart + count;
        feed.dataset.windowStart=String(windowStart);
        feed.dataset.nextOffset=String(end);
        feed.dataset.totalModelCount=String(total);
        feed.dataset.hasMore=end < total ? "true" : "false";
        bottomSentinel.classList.toggle("complete", end >= total);
        if(topSentinel){
            topSentinel.classList.toggle("complete", windowStart <= 0);
        }
    }

    function preserveViewportBy(delta){
        const amount=Number(delta || 0);
        if(!Number.isFinite(amount) || Math.abs(amount) < 0.5) return;
        adjustingScroll=true;
        window.scrollBy(0,amount);
        requestAnimationFrame(()=>{
            lastScrollY=Math.max(0,window.scrollY || 0);
            adjustingScroll=false;
        });
    }

    function releaseCard(card){
        try{ window.modelRadarReleaseCardVideoPreviews?.(card); }catch(_){ }
    }

    function removeCards(cards){
        cards.forEach(card=>{
            releaseCard(card);
            card.remove();
        });
    }

    // Removing cards above the viewport changes document height. Anchor the
    // first card that remains so the user sees no visual jump while the old
    // chunk is discarded.
    function pruneTopToLimit(){
        const cards=mountedCards();
        const overflow=Math.max(0,cards.length-maxMountedCards);
        if(!overflow) return 0;

        const anchor=cards[overflow] || null;
        const before=anchor?.getBoundingClientRect().top ?? null;
        removeCards(cards.slice(0,overflow));
        windowStart += overflow;

        if(anchor && before !== null && anchor.isConnected){
            const after=anchor.getBoundingClientRect().top;
            preserveViewportBy(after-before);
        }
        return overflow;
    }

    function pruneBottomToLimit(){
        const cards=mountedCards();
        const overflow=Math.max(0,cards.length-maxMountedCards);
        if(!overflow) return 0;
        removeCards(cards.slice(cards.length-overflow));
        return overflow;
    }

    function currentStructuralFilters(){
        const sourceInputs=Array.from(document.querySelectorAll('input[name="sources"]'));
        const optionSources=sourceInputs.filter(input => input.checked).map(input => input.value);
        const searchSources=window.modelRadarGetBackendSourceFilters?.() || [];

        return {
            architecture: document.getElementById("familyFilter")?.value || "",
            modelType: document.getElementById("modelTypeFilter")?.value || "",
            status: document.getElementById("statusFilter")?.value || "all",
            favorite: document.getElementById("favoriteFilter")?.value || "all",
            creatorFavorite: document.getElementById("creatorFavoriteFilter")?.value || "all",
            downloadStatus: document.getElementById("downloadStatusFilter")?.value || "all",
            searchText: window.modelRadarGetBackendSearchText?.() || "",
            media: document.getElementById("showMediaOnly")?.checked || false,
            mature: document.getElementById("sensitiveFilter")?.value || "hide",
            sources: searchSources.length ? searchSources : optionSources,
            allSourceCount: sourceInputs.length,
            sourceSearchActive: searchSources.length > 0,
            sort: document.getElementById("sortFilter")?.value || ""
        };
    }

    function applyStructuralParams(url){
        const state=currentStructuralFilters();

        if(state.architecture) url.searchParams.set("architecture",state.architecture);
        if(state.modelType) url.searchParams.set("model_type",state.modelType);
        if(state.status && String(state.status).toLowerCase() !== "all") url.searchParams.set("status",state.status);
        if(state.favorite && String(state.favorite).toLowerCase() !== "all") url.searchParams.set("favorite",state.favorite);
        if(state.creatorFavorite && String(state.creatorFavorite).toLowerCase() !== "all") url.searchParams.set("creator_favorite",state.creatorFavorite);
        if(state.downloadStatus && ["downloaded","updates","not_downloaded"].includes(String(state.downloadStatus).toLowerCase())) url.searchParams.set("download_status",state.downloadStatus);
        if(state.searchText) url.searchParams.set("search",state.searchText);
        if(state.media) url.searchParams.set("media","1");
        url.searchParams.set("mature",String(state.mature || "hide"));
        if(
            state.sourceSearchActive
            || (state.allSourceCount && state.sources.length !== state.allSourceCount)
        ) url.searchParams.set("sources",state.sources.join(","));
        if(state.sort) url.searchParams.set("sort",state.sort);

        const current=new URL(window.location.href);
        ["architecture","model_type","status","sort"].forEach(key=>{
            if(!url.searchParams.has(key) && current.searchParams.has(key)){
                url.searchParams.set(key,current.searchParams.get(key));
            }
        });
    }

    function htmlToFragment(html){
        const template=document.createElement("template");
        template.innerHTML=String(html || "").trim();
        return template.content;
    }

    async function fetchChunk(offset,{mode="append",limit=chunkSize,preserveScroll=false}={}){
        const replace=mode === "replace";
        if(loading && !replace) return null;

        if(replace){
            requestGeneration += 1;
            activeController?.abort();
        }

        const generation=requestGeneration;
        const controller=new AbortController();
        activeController=controller;
        loading=true;
        const activeSentinel=mode === "prepend" ? topSentinel : bottomSentinel;
        activeSentinel?.classList.add("loading");
        activeSentinel?.classList.remove("error");

        try{
            const url=new URL("/feed/chunk",window.location.origin);
            url.searchParams.set("offset",String(Math.max(0,offset)));
            url.searchParams.set("limit",String(limit));
            applyStructuralParams(url);

            const response=await fetch(url,{cache:"no-store",signal:controller.signal});
            const data=await response.json();

            if(generation !== requestGeneration) return null;
            if(!response.ok || !data.success){
                throw new Error(data.error || "Unable to load models.");
            }

            total=Number(data.total || 0);

            let preservedScrollY=null;
            if(mode === "replace"){
                // Initial persisted-filter hydration can finish a few seconds after
                // page load. If the user has already started scrolling, replacing
                // the server-rendered grid must not yank them back to the top.
                preservedScrollY=preserveScroll ? Math.max(0,window.scrollY || 0) : null;
                removeCards(mountedCards());
                windowStart=Number(data.offset || 0);
                if(data.html) grid.appendChild(htmlToFragment(data.html));
            }else if(mode === "append"){
                const existingIds=new Set(mountedCards().map(card=>String(card.dataset.id || "")));
                const marker=document.createElement("div");
                marker.hidden=true;
                grid.appendChild(marker);
                if(data.html){
                    const fragment=htmlToFragment(data.html);
                    Array.from(fragment.querySelectorAll?.(".model-card") || []).forEach(card=>{
                        if(existingIds.has(String(card.dataset.id || ""))) card.remove();
                    });
                    marker.replaceWith(fragment);
                }else{
                    marker.remove();
                }
                pruneTopToLimit();
            }else if(mode === "prepend"){
                const existingCards=mountedCards();
                const existingIds=new Set(existingCards.map(card=>String(card.dataset.id || "")));
                const anchor=existingCards[0] || null;
                const before=anchor?.getBoundingClientRect().top ?? null;
                const fragment=htmlToFragment(data.html);
                Array.from(fragment.querySelectorAll?.(".model-card") || []).forEach(card=>{
                    if(existingIds.has(String(card.dataset.id || ""))) card.remove();
                });
                grid.prepend(fragment);
                windowStart=Number(data.offset || 0);

                if(anchor && before !== null && anchor.isConnected){
                    const after=anchor.getBoundingClientRect().top;
                    preserveViewportBy(after-before);
                }
                pruneBottomToLimit();
            }

            // Publish the new server total before filters.js refreshes the
            // navbar. Otherwise an All/New/Updated shortcut can briefly reuse
            // the previous window's total and require a second click.
            syncFeedState();
            initializeCardVideoPreviews();
            if(typeof window.modelRadarFilterCards === "function") window.modelRadarFilterCards();
            if(preservedScrollY !== null){
                const restorePreservedScroll=()=>window.scrollTo(
                    0,
                    Math.min(
                        preservedScrollY,
                        Math.max(0,document.documentElement.scrollHeight-window.innerHeight)
                    )
                );
                restorePreservedScroll();
                requestAnimationFrame(restorePreservedScroll);
            }
            return data;
        }catch(error){
            if(error?.name === "AbortError") return null;
            if(generation !== requestGeneration) return null;
            console.error("AbyssBeacon feed chunk failed:",error);
            activeSentinel?.classList.add("error");
            return null;
        }finally{
            if(generation === requestGeneration){
                loading=false;
                activeController=null;
                activeSentinel?.classList.remove("loading");
            }
        }
    }

    async function loadNextChunk(){
        if(isFeedWindowSuspended() || loading || windowEnd() >= total) return null;
        return fetchChunk(windowEnd(),{mode:"append",limit:chunkSize});
    }

    async function loadPreviousChunk(){
        if(isFeedWindowSuspended() || loading || windowStart <= 0) return null;
        const offset=Math.max(0,windowStart-chunkSize);
        const limit=Math.max(1,windowStart-offset);
        return fetchChunk(offset,{mode:"prepend",limit});
    }

    async function resetFeedWindow({preserveScroll=false}={}){
        bottomSentinel.classList.remove("complete","error");
        topSentinel?.classList.remove("error");
        const data=await fetchChunk(0,{mode:"replace",limit:replaceSize,preserveScroll});
        syncFeedState();
        return data;
    }

    async function jumpFeedToTop(){
        if(windowStart > 0){
            await fetchChunk(0,{mode:"replace",limit:replaceSize});
        }
        window.scrollTo({top:0,behavior:"smooth"});
    }

    function reconcileAfterRemoval(removedCount=0){
        const removed=Math.max(0,Number(removedCount || 0));
        if(removed) total=Math.max(0,total-removed);
        syncFeedState();
        if(windowEnd() < total && mountedCount() < replaceSize){
            loadNextChunk();
        }
    }

    if("IntersectionObserver" in window){
        const bottomObserver=new IntersectionObserver(entries=>{
            if(isFeedWindowSuspended()) return;
            if(scrollDirection === "down" && entries.some(entry=>entry.isIntersecting)) loadNextChunk();
        },{rootMargin:"1800px 0px",threshold:0.01});
        bottomObserver.observe(bottomSentinel);

        if(topSentinel){
            const topObserver=new IntersectionObserver(entries=>{
                if(isFeedWindowSuspended()) return;
                if(scrollDirection === "up" && entries.some(entry=>entry.isIntersecting)) loadPreviousChunk();
            },{rootMargin:"1800px 0px",threshold:0.01});
            topObserver.observe(topSentinel);
        }
    }

    // Firefox middle-mouse autoscroll can move faster than an observer callback.
    // Keep simple edge checks as a backup in both directions.
    window.addEventListener("scroll",()=>{
        if(isFeedWindowSuspended()) return;
        const currentY=Math.max(0,window.scrollY || 0);
        if(!adjustingScroll){
            if(currentY > lastScrollY + 1) scrollDirection="down";
            else if(currentY < lastScrollY - 1) scrollDirection="up";
            lastScrollY=currentY;
        }

        if(scrollDirection === "down" && window.innerHeight + currentY >= document.documentElement.scrollHeight - 1800){
            loadNextChunk();
        }
        if(scrollDirection === "up" && windowStart > 0 && currentY <= 1800){
            loadPreviousChunk();
        }
    },{passive:true});

    window.modelRadarLoadNextFeedChunk=loadNextChunk;
    window.modelRadarLoadPreviousFeedChunk=loadPreviousChunk;
    window.modelRadarSetFeedWindowSuspended=setFeedWindowSuspended;
    window.modelRadarResetFeedWindow=resetFeedWindow;
    window.modelRadarReconcileFeedWindow=reconcileAfterRemoval;
    window.modelRadarJumpFeedToTop=jumpFeedToTop;

    syncFeedState();

    // A browser reload should always restart AbyssBeacon at the top. The bounded
    // Feed window is rebuilt from the first 120 cards on a deliberate reload.
    try{
        const navigation=performance.getEntriesByType?.("navigation")?.[0];
        if(navigation?.type === "reload"){
            try{ sessionStorage.removeItem(feedRestorePendingKey); }catch(_){ }
            if("scrollRestoration" in history) history.scrollRestoration="manual";
            const forceReloadTop=()=>window.scrollTo(0,0);
            forceReloadTop();
            requestAnimationFrame(forceReloadTop);
            window.addEventListener("pageshow",forceReloadTop,{once:true});
        }
    }catch(_){ }

    const initialState=currentStructuralFilters();
    const needsFilteredWindow =
        Boolean(initialState.architecture) ||
        Boolean(initialState.modelType) ||
        String(initialState.status).toLowerCase() !== "all" ||
        String(initialState.favorite).toLowerCase() !== "all" ||
        String(initialState.creatorFavorite).toLowerCase() !== "all" ||
        ["downloaded","updates","not_downloaded"].includes(String(initialState.downloadStatus).toLowerCase()) ||
        Boolean(initialState.searchText) ||
        initialState.sourceSearchActive ||
        initialState.media ||
        Boolean(initialState.sort) ||
        (initialState.allSourceCount && initialState.sources.length !== initialState.allSourceCount);
    const initialWindowReady = needsFilteredWindow
        ? resetFeedWindow({preserveScroll:true})
        : Promise.resolve(null);

    async function restoreFeedReturnPosition(){
        let shouldRestore=false;
        let target=0;
        let targetWindowStart=0;
        try{
            shouldRestore=sessionStorage.getItem(feedRestorePendingKey)==="1";
            target=Math.max(0,Number.parseInt(sessionStorage.getItem(feedReturnScrollKey)||"0",10)||0);
            targetWindowStart=Math.max(0,Number.parseInt(sessionStorage.getItem(feedReturnWindowStartKey)||"0",10)||0);
            if(shouldRestore) sessionStorage.removeItem(feedRestorePendingKey);
        }catch(_){ }
        if(!shouldRestore) return;

        if("scrollRestoration" in history) history.scrollRestoration="manual";
        try{ await initialWindowReady; }catch(_){ }

        // With a bounded Feed, scrollY is relative to the current 120-card
        // window. Restore that exact server offset instead of rebuilding every
        // chunk that once appeared above it.
        if(targetWindowStart !== windowStart){
            try{
                await fetchChunk(targetWindowStart,{mode:"replace",limit:replaceSize});
            }catch(_){ }
        }

        const restore=()=>window.scrollTo(0,Math.min(target,Math.max(0,document.documentElement.scrollHeight-window.innerHeight)));
        restore();
        requestAnimationFrame(()=>{
            restore();
            requestAnimationFrame(restore);
        });
        setTimeout(restore,120);
        setTimeout(restore,350);
    }

    restoreFeedReturnPosition();
}
