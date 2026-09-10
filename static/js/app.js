document.addEventListener("DOMContentLoaded", () => {

    initializeScanner();
    initializeFilters();
    initializeNavbar();
    initializeSettings();
    initializeModal();
    initializeGallery();
    initializeFullscreen();
    initializeBackToTop();
    initializeCardVideoPreviews();
    initializeFeedWindowing();

});

function initializeBackToTop(){
    const button=document.getElementById("feedBackToTop");
    if(!button) return;
    const update=()=>button.classList.toggle("visible", window.scrollY > 700);
    window.addEventListener("scroll", update, {passive:true});
    button.addEventListener("click", async ()=>{
        if(typeof window.modelRadarJumpFeedToTop === "function"){
            await window.modelRadarJumpFeedToTop();
            return;
        }
        window.scrollTo({top:0, behavior:"smooth"});
    });
    update();
}

let cardVideoPreviewObserver=null;

function getCardVideoPreviewObserver(){
    if(cardVideoPreviewObserver || !("IntersectionObserver" in window)) return cardVideoPreviewObserver;

    cardVideoPreviewObserver=new IntersectionObserver(entries=>{
        entries.forEach(entry=>{
            const video=entry.target;
            if(entry.isIntersecting){
                if(!video.src){
                    const src=video.dataset.src||"";
                    if(!src) return;
                    video.src=src;
                    video.load();
                }
                video.muted=true;
                const pending=video.play();
                if(pending && typeof pending.catch==="function") pending.catch(()=>{});
            }else{
                video.pause();
            }
        });
    },{rootMargin:"250px 0px",threshold:0.05});

    return cardVideoPreviewObserver;
}

function releaseCardVideoPreviews(root){
    const videos=[];
    if(root?.matches?.("video.card-preview-video[data-src]")) videos.push(root);
    root?.querySelectorAll?.("video.card-preview-video[data-src]").forEach(video=>videos.push(video));

    videos.forEach(video=>{
        try{ cardVideoPreviewObserver?.unobserve(video); }catch(_){ }
        try{ video.pause(); }catch(_){ }
        try{
            video.removeAttribute("src");
            video.load();
        }catch(_){ }
    });
}

window.modelRadarReleaseCardVideoPreviews=releaseCardVideoPreviews;

function initializeCardVideoPreviews(){
    const videos=Array.from(document.querySelectorAll("video.card-preview-video[data-src]"))
        .filter(video => video.dataset.previewBound !== "1");
    videos.forEach(video => video.dataset.previewBound = "1");
    if(!videos.length) return;

    const loadAndPlay=video=>{
        if(!video.src){
            const src=video.dataset.src||"";
            if(!src) return;
            video.src=src;
            video.load();
        }
        video.muted=true;
        const pending=video.play();
        if(pending && typeof pending.catch==="function") pending.catch(()=>{});
    };

    videos.forEach(video=>{
        video.addEventListener("error",()=>{
            const poster=String(video.getAttribute("poster")||"");

            if(video.dataset.previewRetry!=="1"){
                const src=video.dataset.src||"";
                if(src){
                    video.dataset.previewRetry="1";
                    setTimeout(()=>{
                        try{
                            video.removeAttribute("src");
                            video.load();
                            video.src=src;
                            video.load();
                            if(video.getBoundingClientRect().bottom>=-250 && video.getBoundingClientRect().top<=window.innerHeight+250){
                                video.muted=true;
                                const pending=video.play();
                                if(pending && typeof pending.catch==="function") pending.catch(()=>{});
                            }
                        }catch(e){}
                    },650);
                    return;
                }
            }

            // If Red's video representation is unusable but its poster is good,
            // keep the card useful instead of leaving a black/broken video.
            if(poster && video.dataset.posterFallback!=="1"){
                video.dataset.posterFallback="1";
                try{ cardVideoPreviewObserver?.unobserve(video); }catch(_){ }
                const img=document.createElement("img");
                img.loading="lazy";
                img.decoding="async";
                img.src=poster;
                img.alt=video.getAttribute("aria-label")||"Video preview";
                img.referrerPolicy="no-referrer";
                img.className="card-preview-video-poster";
                video.replaceWith(img);
            }
        });
    });

    if(!("IntersectionObserver" in window)){
        videos.forEach(loadAndPlay);
        return;
    }

    const observer=getCardVideoPreviewObserver();
    videos.forEach(video=>observer.observe(video));
}

