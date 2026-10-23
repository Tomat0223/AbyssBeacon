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
    let scrollDirection=null;
    let lastScrollY=Math.max(0,window.scrollY || 0);
    let adjustingScroll=false;
    let feedWindowSuspended=false;
    let scrollAdjustmentGeneration=0;
    let positionOperation=0;
    let positioning=true;
    let departing=false;
    const feedSnapshotKey="abyss_feed_snapshot_v2";
    if("scrollRestoration" in history) history.scrollRestoration="manual";
    // The feed owns instant layout compensation. The global smooth-scroll
    // rule can otherwise cross another paging boundary before reaching zero.
    document.documentElement.style.scrollBehavior="auto";

    // Windowing owns scroll compensation here. Native anchoring must not also
    // move the viewport when the same cards are removed or prepended.
    feed.style.overflowAnchor="none";
    // Loading/error text must not resize the top sentinel above mounted cards.
    [topSentinel,bottomSentinel].forEach(sentinel=>{
        if(sentinel) sentinel.style.height="28px";
    });

    function isFeedWindowSuspended(){
        return departing || feedWindowSuspended
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
        if(isFeedWindowSuspended() || positioning) return;
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
    function saveFeedSnapshot(){
        const copy=grid.cloneNode(true);
        copy.querySelectorAll("video").forEach(video=>{
            video.removeAttribute("src");delete video.dataset.previewBound;
        });
        const anchor=captureViewport().find(item=>item.bottom > 0 && item.top < window.innerHeight);
        const state={url:window.location.href,html:copy.innerHTML,
            start:windowStart,total,dataset:{...feed.dataset},
            y:Math.max(0,window.scrollY || 0),
            anchor:anchor ? {id:anchor.card.dataset.id,top:anchor.top} : null,
            filters:window.modelRadarCaptureFeedFilters?.() || null};
        try{
            const text=JSON.stringify(state);
            // This is the mounted window only, never the whole visited feed.
            if(text.length <= 2000000) sessionStorage.setItem(feedSnapshotKey,text);
            else sessionStorage.removeItem(feedSnapshotKey);
            sessionStorage.setItem(feedReturnScrollKey,String(state.y));
            sessionStorage.setItem(feedReturnWindowStartKey,String(windowStart));
        }catch(_){
            try{
                sessionStorage.removeItem(feedSnapshotKey);
                sessionStorage.setItem(feedReturnScrollKey,String(state.y));
                sessionStorage.setItem(feedReturnWindowStartKey,String(windowStart));
            }catch(_){ }
        }
    }

    function prepareFeedDeparture(){
        saveFeedSnapshot();
        try{ sessionStorage.setItem(feedRestorePendingKey,"1"); }catch(_){ }
        // A late response must not replace the window after it was saved.
        requestGeneration += 1;activeController?.abort();loading=false;
        departing=true;
    }
    window.modelRadarPrepareFeedDeparture=prepareFeedDeparture;
    document.addEventListener("click",event=>{
        if(event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
        const link=event.target.closest?.("a[href]");
        if(!link || link.target === "_blank" || link.hasAttribute("download")) return;
        const url=new URL(link.href,window.location.href);
        if(url.origin === window.location.origin && /^\/(creator|collection)\//.test(url.pathname)) prepareFeedDeparture();
    },true);
    window.addEventListener("pagehide",()=>{
        if(!departing) saveFeedSnapshot();
    });
    window.addEventListener("pageshow",event=>{
        if(!event.persisted) return;
        departing=false;positioning=true;
        window.modelRadarCloseModelOverlay?.();
        try{ sessionStorage.removeItem(feedRestorePendingKey); }catch(_){ }
        finishPositioning(++positionOperation);
    });

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
        const generation=++scrollAdjustmentGeneration;
        adjustingScroll=true;
        if(Number.isFinite(amount) && Math.abs(amount) >= 0.5){
            window.scrollBy({top:amount,left:0,behavior:"instant"});
        }
        // Scroll events/observer notifications can arrive on the next frame.
        // Keep synthetic movement out of direction detection until it settles.
        requestAnimationFrame(()=>requestAnimationFrame(()=>{
            if(generation !== scrollAdjustmentGeneration) return;
            lastScrollY=Math.max(0,window.scrollY || 0);
            adjustingScroll=false;
        }));
    }

    function visibleCard(card){
        return card.getClientRects().length > 0;
    }

    function captureViewport(){
        return mountedCards().filter(visibleCard).map(card=>{
            const rect=card.getBoundingClientRect();
            return {card,top:rect.top,bottom:rect.bottom};
        });
    }

    function restoreViewport(anchors){
        const surviving=anchors.filter(anchor=>anchor.card.isConnected && visibleCard(anchor.card));
        const anchor=surviving.find(item=>item.bottom > 0 && item.top < window.innerHeight)
            || surviving[0];
        preserveViewportBy(anchor ? anchor.card.getBoundingClientRect().top-anchor.top : 0);
    }

    function rowChunkSize(){
        const cards=mountedCards().filter(visibleCard);
        const first=cards[0];
        const columns=first
            ? cards.filter(card=>card.offsetTop === first.offsetTop).length
            : 1;
        return Math.max(columns,Math.floor(chunkSize/columns)*columns);
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

    // Trim whole visual rows: removing 80 cards from a three-column grid
    // otherwise moves retained models into different columns on every page.
    function pruneTopToLimit(protectedCards){
        const cards=mountedCards();
        let overflow=Math.max(0,cards.length-maxMountedCards);
        if(!overflow) return 0;
        const previous=cards.slice(0,overflow).reverse().find(visibleCard);
        if(previous){
            while(overflow < cards.length && (
                !visibleCard(cards[overflow]) || cards[overflow].offsetTop === previous.offsetTop
            )) overflow += 1;
        }
        // The user may have reversed direction while the request was in
        // flight. Discard the new tail instead of removing a visible model.
        if(cards.slice(0,overflow).some(card=>protectedCards.has(card))){
            removeCards(cards.slice(maxMountedCards));
            return 0;
        }
        removeCards(cards.slice(0,overflow));
        windowStart += overflow;
        return overflow;
    }

    function pruneBottomToLimit(protectedCards){
        const cards=mountedCards();
        const overflow=Math.max(0,cards.length-maxMountedCards);
        if(!overflow) return 0;
        if(cards.slice(cards.length-overflow).some(card=>protectedCards.has(card))) return pruneTopToLimit(protectedCards);
        removeCards(cards.slice(cards.length-overflow));
        return overflow;
    }

    function currentStructuralFilters(){
        const sourceInputs=Array.from(document.querySelectorAll('input[name="sources"]'));
        const optionSources=sourceInputs.filter(input => input.checked).map(input => input.value);
        const searchSources=window.modelRadarGetBackendSourceFilters?.() || [];

        return {
            architecture: window.modelRadarArchitectureFilterValue?.() || "",
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

            const viewport=mode === "replace" ? null : captureViewport();
            const protectedCards=new Set((viewport || [])
                .filter(anchor=>anchor.bottom > 0 && anchor.top < window.innerHeight)
                .map(anchor=>anchor.card));
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
                pruneTopToLimit(protectedCards);
            }else if(mode === "prepend"){
                const existingCards=mountedCards();
                const existingIds=new Set(existingCards.map(card=>String(card.dataset.id || "")));
                const fragment=htmlToFragment(data.html);
                Array.from(fragment.querySelectorAll?.(".model-card") || []).forEach(card=>{
                    if(existingIds.has(String(card.dataset.id || ""))) card.remove();
                });
                grid.prepend(fragment);
                windowStart=Number(data.offset || 0);

                pruneBottomToLimit(protectedCards);
            }

            // Publish the new server total before filters.js refreshes the
            // navbar. Otherwise an All/New/Updated shortcut can briefly reuse
            // the previous window's total and require a second click.
            syncFeedState();
            initializeCardVideoPreviews();
            if(typeof window.modelRadarFilterCards === "function") window.modelRadarFilterCards();
            // Include pruning, filtering and top-sentinel visibility in the same
            // measurement, rather than compensating an intermediate layout.
            if(viewport) restoreViewport(viewport);
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
        if(isFeedWindowSuspended() || positioning || adjustingScroll || loading || windowEnd() >= total) return null;
        return fetchChunk(windowEnd(),{mode:"append",limit:rowChunkSize()});
    }

    async function loadPreviousChunk(){
        if(isFeedWindowSuspended() || positioning || adjustingScroll || loading || windowStart <= 0) return null;
        const offset=Math.max(0,windowStart-rowChunkSize());
        const limit=Math.max(1,windowStart-offset);
        return fetchChunk(offset,{mode:"prepend",limit});
    }

    function finishPositioning(operation){
        requestAnimationFrame(()=>requestAnimationFrame(()=>{
            if(operation !== positionOperation) return;
            lastScrollY=Math.max(0,window.scrollY || 0);
            scrollDirection=null;
            positioning=false;
            queueFeedReturnPositionSave();
        }));
    }

    async function resetFeedWindow({preserveScroll=false}={}){
        const operation=++positionOperation;positioning=true;
        bottomSentinel.classList.remove("complete","error");
        topSentinel?.classList.remove("error");
        const data=await fetchChunk(0,{mode:"replace",limit:replaceSize,preserveScroll});
        if(operation === positionOperation && !preserveScroll) window.scrollTo({top:0,behavior:"instant"});
        finishPositioning(operation);
        return data;
    }

    async function jumpFeedToTop(){
        const operation=++positionOperation;positioning=true;
        // Replacing cancels a pending prepend, even if the user reached the
        // top while that older request was still loading.
        if(windowStart > 0 || loading){
            await fetchChunk(0,{mode:"replace",limit:replaceSize});
        }
        if(operation === positionOperation){
            window.scrollTo({top:0,behavior:"instant"});
            finishPositioning(operation);
        }
    }

    async function reconcileAfterRemoval(removedCount=0,{all=false}={}){
        const operation=++positionOperation;positioning=true;
        total=Math.max(0,total-Math.max(0,Number(removedCount || 0)));
        const offset=all ? 0 : Math.min(windowStart,Math.max(0,total-replaceSize));
        await fetchChunk(offset,{mode:"replace",limit:replaceSize,preserveScroll:!all});
        if(operation === positionOperation && (all || total === 0)) window.scrollTo({top:0,behavior:"instant"});
        finishPositioning(operation);
    }

    if("IntersectionObserver" in window){
        const bottomObserver=new IntersectionObserver(entries=>{
            if(isFeedWindowSuspended() || positioning || adjustingScroll || loading) return;
            if(scrollDirection === "down" && entries.some(entry=>entry.isIntersecting)) loadNextChunk();
        },{rootMargin:"1800px 0px",threshold:0.01});
        bottomObserver.observe(bottomSentinel);

        if(topSentinel){
            const topObserver=new IntersectionObserver(entries=>{
                if(isFeedWindowSuspended() || positioning || adjustingScroll || loading) return;
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
        // Reaching zero is an explicit request for the beginning, including
        // the small interval in which an earlier chunk is compensating scroll.
        if(!positioning && currentY <= 1 && windowStart > 0 && lastScrollY > 1){
            scrollAdjustmentGeneration += 1;adjustingScroll=false;
            jumpFeedToTop();
            return;
        }
        if(adjustingScroll || positioning){
            lastScrollY=currentY;
            return;
        }
        if(currentY > lastScrollY + 1) scrollDirection="down";
        else if(currentY < lastScrollY - 1) scrollDirection="up";
        lastScrollY=currentY;
        if(currentY <= 1 && windowStart > 0 && scrollDirection === "up"){
            jumpFeedToTop();
            return;
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

    let restoreRequested=false;
    let snapshot=null;
    try{
        const navigation=performance.getEntriesByType?.("navigation")?.[0];
        restoreRequested=navigation?.type !== "reload"
            && sessionStorage.getItem(feedRestorePendingKey) === "1";
        if(restoreRequested) snapshot=JSON.parse(sessionStorage.getItem(feedSnapshotKey) || "null");
        sessionStorage.removeItem(feedRestorePendingKey);
        if(snapshot){
            const url=new URL(snapshot.url);
            if(url.origin !== window.location.origin || url.pathname !== "/" || typeof snapshot.html !== "string"
                || snapshot.html.length > 2000000 || !Number.isInteger(snapshot.start) || snapshot.start < 0
                || !Number.isFinite(snapshot.y)) snapshot=null;
        }
    }catch(_){ snapshot=null; }

    if(snapshot){
        const fragment=htmlToFragment(snapshot.html);
        if(fragment.querySelectorAll(".model-card").length > maxMountedCards) snapshot=null;
        else{
            removeCards(mountedCards());grid.replaceChildren(fragment);
            Object.assign(feed.dataset,snapshot.dataset || {});
            windowStart=snapshot.start;total=Math.max(0,Number(snapshot.total) || 0);
            window.modelRadarRestoreFeedFilters?.(snapshot.filters);
            syncFeedState();initializeCardVideoPreviews();window.modelRadarFilterCards?.();
        }
    }

    const initialState=currentStructuralFilters();
    const needsFilteredWindow =
        Boolean(initialState.architecture) || Boolean(initialState.modelType) ||
        String(initialState.status).toLowerCase() !== "all" ||
        String(initialState.favorite).toLowerCase() !== "all" ||
        String(initialState.creatorFavorite).toLowerCase() !== "all" ||
        ["downloaded","updates","not_downloaded"].includes(String(initialState.downloadStatus).toLowerCase()) ||
        Boolean(initialState.searchText) || initialState.sourceSearchActive || initialState.media || Boolean(initialState.sort) ||
        (initialState.allSourceCount && initialState.sources.length !== initialState.allSourceCount);

    async function initializePosition(){
        const operation=++positionOperation;
        if(!snapshot && needsFilteredWindow){
            await fetchChunk(0,{mode:"replace",limit:replaceSize});
        }
        if(operation !== positionOperation) return;
        if(restoreRequested){
            let target=snapshot?.y || 0;
            let start=snapshot?.start || 0;
            if(!snapshot){
                try{
                    target=Math.max(0,Number(sessionStorage.getItem(feedReturnScrollKey)) || 0);
                    start=Math.max(0,Number(sessionStorage.getItem(feedReturnWindowStartKey)) || 0);
                }catch(_){ }
                if(start !== windowStart) await fetchChunk(start,{mode:"replace",limit:replaceSize});
            }
            if(operation !== positionOperation) return;
            window.scrollTo({top:target,behavior:"instant"});
            if(snapshot?.anchor){
                const card=mountedCards().find(item=>item.dataset.id === snapshot.anchor.id);
                if(card) window.scrollBy({top:card.getBoundingClientRect().top-snapshot.anchor.top,behavior:"instant"});
            }
        }else{
            window.scrollTo({top:0,behavior:"instant"});
        }
        finishPositioning(operation);
    }
    initializePosition();
}
