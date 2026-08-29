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

    async function fetchChunk(offset,{mode="append",limit=chunkSize}={}){
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
        const activeSentinel=bottomSentinel;
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

            if(mode === "replace"){
                grid.replaceChildren();
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

            }

            // Publish the new server total before filters.js refreshes the
            // navbar. Otherwise an All/New/Updated shortcut can briefly reuse
            // the previous window's total and require a second click.
            syncFeedState();
            initializeCardVideoPreviews();
            if(typeof window.modelRadarFilterCards === "function") window.modelRadarFilterCards();
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
        if(loading || windowEnd() >= total) return null;
        return fetchChunk(windowEnd(),{mode:"append",limit:chunkSize});
    }

    async function resetFeedWindow(){
        bottomSentinel.classList.remove("complete","error");
        topSentinel?.classList.remove("error");
        const data=await fetchChunk(0,{mode:"replace",limit:replaceSize});
        syncFeedState();
        return data;
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
            if(entries.some(entry=>entry.isIntersecting)) loadNextChunk();
        },{rootMargin:"1800px 0px",threshold:0.01});
        bottomObserver.observe(bottomSentinel);
    }

    // Firefox middle-mouse autoscroll can move faster than an observer callback.
    // Keep a simple downward edge check as a backup. There is intentionally no
    // upward paging: once cards are loaded they remain mounted until reload or a
    // structural filter replaces the feed.
    window.addEventListener("scroll",()=>{
        if(window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 1800){
            loadNextChunk();
        }
    },{passive:true});

    window.modelRadarLoadNextFeedChunk=loadNextChunk;
    window.modelRadarResetFeedWindow=resetFeedWindow;
    window.modelRadarReconcileFeedWindow=reconcileAfterRemoval;

    syncFeedState();

    // A browser reload should always restart AbyssBeacon at the top. Loaded cards
    // are deliberately kept for the life of the page, so reload is the clean
    // reset point instead of rebuilding old chunks above the user.
    try{
        const navigation=performance.getEntriesByType?.("navigation")?.[0];
        if(navigation?.type === "reload"){
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
    if(needsFilteredWindow) resetFeedWindow();
}
