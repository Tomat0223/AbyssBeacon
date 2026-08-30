function initializeGallery(){
    const detail = document.querySelector(".model-detail");
    if(!detail) return;

    // Every model card gets its own gallery lifecycle. The modal reuses the
    // same outer panel, so global/panel listeners must be removed when this
    // detail is closed or replaced.
    const lifecycle = new AbortController();
    const lifecycleSignal = lifecycle.signal;

    const prev = detail.querySelector(".media-prev");
    const next = detail.querySelector(".media-next");
    const counter = detail.querySelector(".media-counter");
    const filenameEl = detail.querySelector(".media-filename");
    const pathEl = detail.querySelector(".media-path");
    const metadataCard = detail.querySelector(".media-metadata-card");
    const metadataBody = detail.querySelector(".media-metadata-body");
    const metadataToggle = detail.querySelector(".media-metadata-toggle");
    const metadataHide = detail.querySelector(".media-metadata-hide");
    const metadataRestore = detail.querySelector(".media-metadata-restore");
    const folderBtn = detail.querySelector(".media-folder-btn");
    const downloadModelBtn = detail.querySelector(".media-download-model-btn");
    const sourceViewSelect = detail.querySelector(".media-source-view-select");
    const expandBtn = detail.querySelector(".media-expand-btn");
    const filesPanel = detail.querySelector(".media-files-panel");
    const filesList = detail.querySelector(".media-files-list");
    const filesFolder = detail.querySelector(".media-files-folder");
    const filesTitle = detail.querySelector(".media-files-title");
    const filesClose = detail.querySelector(".media-files-close");
    const container = detail.querySelector(".media-container");
    const virtualStage = detail.querySelector(".media-virtual-stage");
    const panel = detail.closest(".model-panel");

    function readJson(selector, fallback){
        const el = detail.querySelector(selector);
        if(!el) return fallback;
        try { return JSON.parse(el.textContent || "") || fallback; }
        catch(e){ return fallback; }
    }

    const mediaData = readJson(".media-data-json", []);
    const modelFiles = readJson(".model-files-json", []);
    const downloadSources = readJson(".download-sources-json", []);
    const fallbackItems = Array.from(detail.querySelectorAll(".media-item"));
    let activeSourceView=String(sourceViewSelect?.value||"combined").trim().toLowerCase()||"combined";

    function sourceMatchesIndex(index){
        if(activeSourceView==="combined") return true;
        return String(dataFor(index).source||"").trim().toLowerCase()===activeSourceView;
    }

    function mediaIdentityKey(index){
        const data=dataFor(index);
        const url=String(data.url||data.thumbnail||"").trim();
        if(!url) return `index:${index}`;
        return url.split("#",1)[0].split("?",1)[0].toLowerCase();
    }

    function dedupeMediaIndices(indices){
        const seen=new Set();
        return indices.filter(index=>{
            const key=mediaIdentityKey(index);
            if(seen.has(key)) return false;
            seen.add(key);
            return true;
        });
    }

    function sourceVisibleIndices(){
        const count=mediaData.length || fallbackItems.length;
        const indices=Array.from({length:count},(_,i)=>i).filter(sourceMatchesIndex);
        return activeSourceView==="combined" ? dedupeMediaIndices(indices) : indices;
    }

    let visibleIndices = sourceVisibleIndices();
    let total = visibleIndices.length;
    if(total === 0) return;
    fallbackItems.filter(item=>item.tagName==="VIDEO").forEach(video=>{
        video.loop=true;
        video.playsInline=true;
        configureVideo(video);
    });
    if(total <= 1){ if(prev) prev.style.display="none"; if(next) next.style.display="none"; }

    let current = 0;
    let currentNode = fallbackItems[0] || null;
    const preloadCache = new Map();
    let zoomLevel=1, panX=0, panY=0, dragging=false, startX=0, startY=0;

    // Audio policy:
    // - every newly opened model starts muted;
    // - mute/unmute is remembered only while this model detail stays open;
    // - volume is a persistent browser preference shared across model cards.
    let cardVideoMuted=true;
    let applyingVideoAudioState=false;

    function savedVideoVolume(){
        try{
            const raw=localStorage.getItem("modelradarVideoVolume");
            if(raw===null) return 0.25;
            const value=Number.parseFloat(raw);
            return Number.isFinite(value)?Math.max(0,Math.min(1,value)):0.25;
        }catch(e){return 0.25;}
    }

    function configureVideo(video){
        if(!video || video.dataset.volumeMemoryBound==="1") return;
        applyingVideoAudioState=true;
        video.volume=savedVideoVolume();
        video.muted=cardVideoMuted;
        applyingVideoAudioState=false;
        video.dataset.volumeMemoryBound="1";

        video.addEventListener("volumechange",()=>{
            if(applyingVideoAudioState) return;
            cardVideoMuted=video.muted;
            try{ localStorage.setItem("modelradarVideoVolume",String(video.volume)); }catch(e){}
        });
    }

    function applyCardVideoAudio(video){
        if(!video || video.tagName!=="VIDEO") return;
        applyingVideoAudioState=true;
        video.volume=savedVideoVolume();
        video.muted=cardVideoMuted;
        applyingVideoAudioState=false;
    }

    function playCurrentVideo(){
        if(!currentNode || currentNode.tagName!=="VIDEO") return;
        applyCardVideoAudio(currentNode);
        const attempt=currentNode.play();
        if(attempt && typeof attempt.catch==="function"){
            attempt.catch(()=>{
                // Browsers always permit muted autoplay. If a later video is
                // blocked because this card was unmuted, fall back to muted
                // playback without changing the user's remembered card choice.
                applyingVideoAudioState=true;
                currentNode.muted=true;
                applyingVideoAudioState=false;
                const mutedAttempt=currentNode.play();
                if(mutedAttempt && typeof mutedAttempt.catch==="function") mutedAttempt.catch(()=>{});
            });
        }
    }

    function pauseCurrentVideo(){
        if(currentNode && currentNode.tagName==="VIDEO") currentNode.pause();
    }

    function dataFor(index){
        const raw = mediaData[index] || {};
        let meta = raw.metadata_obj || raw.metadata || {};
        if(typeof meta === "string"){ try { meta=JSON.parse(meta); } catch(e){ meta={}; } }
        return {...raw, filename:raw.filename||meta.filename||`Preview ${index+1}`, path:raw.path||meta.path||raw.filename||"", metadata:meta||{}};
    }

    function createMediaNode(index){
        const data=dataFor(index);
        const type=String(data.type||"image").toLowerCase();
        let el;
        if(type === "video"){
            el=document.createElement("video"); el.controls=true; el.preload="auto"; el.loop=true; el.playsInline=true;
            el.referrerPolicy="no-referrer";
            const videoUrl=String(data.url||"");
            const posterUrl=String(data.thumbnail||"");
            const posterClean=posterUrl.split("?",1)[0].split("#",1)[0].toLowerCase();
            const posterIsVideo=/\.(mp4|webm|mov|m4v|avi|mkv)$/.test(posterClean);
            if(posterUrl && posterUrl!==videoUrl && !posterIsVideo) el.poster=posterUrl;
            el.src=videoUrl;
            const fallbackVideoUrl=String(data.fallback_url||"");
            el.addEventListener("error",()=>{
                // Some Red uploads reject the optimized/transcoded variant.
                // Try the original source once before giving up on video.
                if(el.dataset.mediaFallback!=="1" && fallbackVideoUrl && fallbackVideoUrl!==videoUrl){
                    el.dataset.mediaFallback="1";
                    try{
                        el.pause();
                        el.removeAttribute("src");
                        el.load();
                        el.src=fallbackVideoUrl;
                        el.load();
                        if(el.classList.contains("active")) playCurrentVideo();
                    }catch(e){}
                    return;
                }

                // Firefox otherwise replaces a valid poster with its native
                // "file is corrupt" screen. Keep the poster as a usable still.
                if(posterUrl && el.dataset.posterFallback!=="1"){
                    el.dataset.posterFallback="1";
                    const img=document.createElement("img");
                    img.className=el.className;
                    img.dataset.index=el.dataset.index||String(index);
                    img.dataset.loaded="1";
                    img.alt=data.filename||"Video preview";
                    img.src=posterUrl;
                    img.addEventListener("dblclick",()=>openFullscreen(img.src));
                    if(currentNode===el) currentNode=img;
                    try{el.pause();}catch(e){}
                    el.replaceWith(img);
                }
            });
            configureVideo(el);
        } else {
            el=document.createElement("img"); el.alt=data.filename||"Model preview"; el.src=data.url||"";
            const fallbackUrl=String(data.fallback_url||"");
            el.addEventListener("error",()=>{
                if(el.dataset.fallbackTried==="1" || !fallbackUrl || el.src===fallbackUrl) return;
                el.dataset.fallbackTried="1";
                el.src=fallbackUrl;
            });
            el.addEventListener("dblclick", ()=>openFullscreen(el.src));
        }
        el.className="media-item active"; el.dataset.index=String(index); el.dataset.loaded="1";
        return el;
    }

    function preload(index){
        if(index<0 || index>=mediaData.length || preloadCache.has(index)) return;
        const data=dataFor(index);
        if(String(data.type||"image").toLowerCase() !== "image" || !data.url) return;
        const img=new Image(); img.src=data.url; preloadCache.set(index,img);
        if(preloadCache.size>10){ const first=preloadCache.keys().next().value; preloadCache.delete(first); }
    }
    function preloadNearby(index){ for(let i=1;i<=3;i++) preload(index+i); preload(index-1); }

    function metadataEntries(data){
        const meta=data.metadata||{}, preferred=["model","Model","checkpoint","prompt","Prompt","negative_prompt","Negative prompt","seed","Seed","steps","Steps","cfg","CFG","sampler","Sampler"], entries=[], used=new Set();
        preferred.forEach(k=>{if(meta[k]!==undefined&&meta[k]!==null&&meta[k]!==""){entries.push([k,meta[k]]);used.add(k);}});
        Object.entries(meta).forEach(([k,v])=>{if(!used.has(k)&&v!==undefined&&v!==null&&v!==""&&k!=="url"&&!k.startsWith("_")) entries.push([k,v]);});
        if(!entries.some(([k])=>k.toLowerCase()==="filename")) entries.unshift(["filename",data.filename]);
        if(data.path&&!entries.some(([k])=>k.toLowerCase()==="path")) entries.splice(1,0,["path",data.path]);
        return entries;
    }
    function valueText(v){ if(typeof v==="object"){try{return JSON.stringify(v);}catch(e){}} return String(v); }
    async function enrichMetadataIfNeeded(index){
        const data=dataFor(index), meta=data.metadata||{};
        if(meta._generation_data_cached||meta._generation_data_loading||!meta.civitai_red_media_id||!data.id) return;
        const modelId=detail.dataset.modelId;
        if(!modelId) return;
        meta._generation_data_loading=true;
        try{
            const response=await fetch(`/api/model/${encodeURIComponent(modelId)}/media/${encodeURIComponent(data.id)}/metadata`);
            const payload=await response.json().catch(()=>({}));
            if(response.ok&&payload.metadata&&typeof payload.metadata==="object"){
                // dataFor() returns a presentation copy. Save enrichment back
                // into the authoritative mediaData entry before rendering.
                if(mediaData[index]){
                    mediaData[index].metadata=payload.metadata;
                    mediaData[index].metadata_obj=payload.metadata;
                }
                renderMetadata(index,false);
            }
        }catch(e){
            // Optional enrichment must never interrupt browsing.
        }finally{
            if(data.metadata) delete data.metadata._generation_data_loading;
        }
    }

    function renderMetadata(index,allowEnrich=true){
        const data=dataFor(index);
        if(filenameEl) filenameEl.textContent=data.filename||"";
        if(pathEl) pathEl.textContent=data.path&&data.path!==data.filename?data.path:"";
        if(!metadataBody) return;
        const entries=metadataEntries(data); metadataBody.innerHTML="";
        entries.forEach(([k,v],i)=>{const row=document.createElement("div");row.className="media-meta-row"+(i>=5?" media-meta-extra":"");const l=document.createElement("span");l.className="media-meta-label";l.textContent=k.replaceAll("_"," ");const val=document.createElement("span");val.className="media-meta-value";val.textContent=valueText(v);row.append(l,val);metadataBody.appendChild(row);});
        if(allowEnrich) enrichMetadataIfNeeded(index);
    }

    function dirname(path){const clean=String(path||"").replaceAll("\\","/");const i=clean.lastIndexOf("/");return i>=0?clean.slice(0,i):"";}
    function filePath(file){return String((file&&(file.path||file.name))||"").replaceAll("\\","/");}
    function isModelFile(file){const p=filePath(file).toLowerCase();return !!file.primary||[".safetensors",".ckpt",".pt",".pth",".bin",".gguf"].some(ext=>p.endsWith(ext));}
    function folderCandidates(index){const folder=dirname(dataFor(index).path);return {folder,files:modelFiles.filter(f=>dirname(filePath(f))===folder)};}
    function formatFileSize(file){
        const labeled=String(file?.size_label||"").trim();
        if(labeled) return labeled;
        const bytes=Number(file?.size_bytes||0);
        if(!Number.isFinite(bytes) || bytes<=0) return "";
        const units=["B","KB","MB","GB","TB"];
        let value=bytes, unit=0;
        while(value>=1024 && unit<units.length-1){value/=1024;unit++;}
        const decimals=unit===0?0:(value>=100?0:value>=10?1:2);
        return `${value.toFixed(decimals)} ${units[unit]}`;
    }

    function updatePreviewModelAction(index){
        if(!downloadModelBtn)return; const candidates=folderCandidates(index).files.filter(isModelFile);
        if(candidates.length===1){downloadModelBtn.textContent="Download Preview Model";downloadModelBtn.title=`Download ${candidates[0].name||filePath(candidates[0]).split("/").pop()}`;downloadModelBtn.dataset.match="exact-folder";}
        else{downloadModelBtn.textContent="Find Preview Model";downloadModelBtn.title=candidates.length>1?`Choose from ${candidates.length} model files in this preview folder.`:"Find model files that may be associated with this preview.";downloadModelBtn.dataset.match="candidates";}
    }
    function sourceLabel(source){
        return ({huggingface:"Hugging Face",modelscope:"ModelScope",civitai:"CivitAI",civitaired:"CivitAI Red",tensorhub:"TensorHub Art",seaart:"SeaArt"})[source]||source||"Source";
    }
    function appendFileRow(file,source,fileIndex,accessStatus){
        const row=document.createElement("div"); row.className="media-folder-file";
        const info=document.createElement("div"); info.className="media-folder-file-info";
        const name=document.createElement("strong"); name.textContent=file.name||filePath(file).split("/").pop()||"Model file";
        const path=document.createElement("span"); path.textContent=filePath(file); info.append(name,path);
        const sizeText=formatFileSize(file); if(sizeText){const size=document.createElement("span");size.className="media-folder-file-size";size.textContent=sizeText;info.appendChild(size);}
        const a=document.createElement("a"); a.className="media-folder-download"; a.textContent="Download"; a.target="_blank"; a.rel="noopener";
        const modelId=detail.dataset.modelId;
        const downloadable=(accessStatus!=="gated")&&(source==="huggingface"||file.download_url||file.model_file_id||file.model_ver_no);
        if(modelId!==undefined&&fileIndex!==undefined&&downloadable){
            a.href=source?`/download/source/${encodeURIComponent(modelId)}/${encodeURIComponent(source)}/${encodeURIComponent(fileIndex)}`:`/download/model/${encodeURIComponent(modelId)}/${encodeURIComponent(fileIndex)}`;
            a.title=accessStatus==="paid_access"
                ?`Download if your account has purchased access on ${sourceLabel(source)}`
                :(source?`Download from ${sourceLabel(source)}`:"Download through AbyssBeacon");
        }else{a.classList.add("disabled");a.removeAttribute("href");a.title="Direct download is unavailable";}
        row.append(info,a); filesList.appendChild(row);
    }
    function appendSourceHeading(src){
        const head=document.createElement("div"); head.className="media-download-source-heading";
        const strong=document.createElement("strong"); strong.textContent=sourceLabel(src.source);
        const state=document.createElement("span"); state.className=`media-download-source-state ${src.access_status||""}`;
        state.textContent=src.access_status==="downloadable"?"↓ Downloadable":src.access_status==="paid_access"?"$ Paid Access":src.access_status==="gated"?"🔒 Restricted":"? Unknown";
        head.append(strong,state); filesList.appendChild(head);
        if(src.source==="seaart"&&src.access_status==="downloadable"){
            const note=document.createElement("div"); note.className="media-download-source-note"; note.textContent="SeaArt requires a signed-in browser session when the download is requested."; filesList.appendChild(note);
        }
    }
    function renderFiles(index,modelOnly){
        if(!filesPanel||!filesList)return;
        const {folder,files}=folderCandidates(index); filesList.innerHTML="";
        const viewDownloadSources=activeSourceView==="combined"
            ? downloadSources
            : downloadSources.filter(src=>String(src?.source||"").trim().toLowerCase()===activeSourceView);
        if(modelOnly && viewDownloadSources.length){
            filesTitle.textContent=viewDownloadSources.length>1?"Choose download source":"Model files for this preview";
            filesFolder.textContent=viewDownloadSources.length>1?"This model is available from multiple sources":(folder?folder+"/":"Available model files");
            let count=0;
            viewDownloadSources.forEach(src=>{
                let srcFiles=Array.isArray(src.files)?src.files:[];
                let primary=srcFiles.map((f,i)=>({f,i})).filter(x=>x.f&&typeof x.f==="object"&&isModelFile(x.f)&&x.f.primary);
                let shown=primary.length?primary:srcFiles.map((f,i)=>({f,i})).filter(x=>x.f&&typeof x.f==="object"&&isModelFile(x.f));
                if(!shown.length)return;
                appendSourceHeading(src); shown.forEach(x=>{appendFileRow(x.f,src.source,x.i,src.access_status);count++;});
            });
            if(!count){const e=document.createElement("div");e.className="media-files-empty";e.textContent="No downloadable model files were found. Rescan the source to refresh its file metadata.";filesList.appendChild(e);}
        }else{
            const sourceRepositoryFiles=(activeSourceView!=="combined" && viewDownloadSources.length===1)
                ? (Array.isArray(viewDownloadSources[0].files)?viewDownloadSources[0].files:[])
                : modelFiles;
            let shown=modelOnly?files.filter(isModelFile):sourceRepositoryFiles;
            if(modelOnly&&shown.length===0)shown=sourceRepositoryFiles.filter(isModelFile);
            filesTitle.textContent=modelOnly?"Model files for this preview":"All repository files";
            filesFolder.textContent=modelOnly?(folder?folder+"/":"Repository root / available files"):"Repository root + subfolders";
            if(!shown.length){const e=document.createElement("div");e.className="media-files-empty";e.textContent="No downloadable files were found here.";filesList.appendChild(e);}
            shown.forEach((file,i)=>appendFileRow(file,"",file._download_index!==undefined?file._download_index:i));
        }
        filesPanel.classList.add("open");
    }

    function actualIndex(){ return visibleIndices[current] ?? current; }

    function showImage(index){
        total=visibleIndices.length;
        if(!total) return;
        if(index<0) index=total-1; if(index>=total) index=0; current=index;
        const actual=actualIndex();
        if(mediaData.length && virtualStage){
            if(currentNode&&currentNode.tagName==="VIDEO") currentNode.pause();
            currentNode=createMediaNode(actual); virtualStage.replaceChildren(currentNode); preload(actual);
        } else {
            fallbackItems.forEach((item,i)=>item.classList.toggle("active",i===actual)); currentNode=fallbackItems[actual];
            if(currentNode&&currentNode.dataset.src&&!currentNode.src){currentNode.src=currentNode.dataset.src;}
        }
        if(counter) counter.textContent=`${current+1} / ${total}`;
        renderMetadata(actual); updatePreviewModelAction(actual); if(filesPanel)filesPanel.classList.remove("open");
        if(currentNode && currentNode.tagName==="VIDEO") playCurrentVideo();
        if(prev) prev.style.display=total<=1?"none":"";
        if(next) next.style.display=total<=1?"none":"";
    }

    function renderEmptyMedia(message="No previews found for this source."){
        pauseCurrentVideo();
        total=0;
        current=0;
        if(virtualStage){
            const empty=document.createElement("div");
            empty.className="media-version-loading";
            empty.textContent=message;
            virtualStage.replaceChildren(empty);
            currentNode=empty;
        }else{
            fallbackItems.forEach(item=>item.classList.remove("active"));
            currentNode=null;
        }
        if(counter) counter.textContent="0 / 0";
        if(prev) prev.style.display="none";
        if(next) next.style.display="none";
        if(filenameEl) filenameEl.textContent="";
        if(pathEl) pathEl.textContent="";
        if(metadataBody) metadataBody.innerHTML="";
        if(filesPanel) filesPanel.classList.remove("open");
    }

    const versionMetadataRequests=new Map();

    function versionIdentity(index){
        const meta=dataFor(index).metadata||{};
        return {
            name:String(
                meta.civitai_model_version||
                meta.civitai_red_model_version||
                meta.modelscope_version_name||
                meta.tensorhub_version_name||
                meta.model_version||""
            ).trim().toLocaleLowerCase(),
            id:String(
                meta.civitai_model_version_id||
                meta.civitai_red_model_version_id||
                meta.modelscope_version_id||
                meta.tensorhub_version_id||
                meta.model_version_id||""
            ).trim()
        };
    }

    function matchingVersionIndices(wantedName,wantedId){
        wantedName=String(wantedName||"").trim().toLocaleLowerCase();
        wantedId=String(wantedId||"").trim();
        const matches=[];
        mediaData.forEach((_,i)=>{
            if(!sourceMatchesIndex(i)) return;
            const identity=versionIdentity(i);
            if((wantedId&&identity.id===wantedId)||(wantedName&&identity.name===wantedName)){
                matches.push(i);
            }
        });
        return activeSourceView==="combined" ? dedupeMediaIndices(matches) : matches;
    }

    function mergeReturnedVersionMedia(items){
        if(!Array.isArray(items)) return [];

        const indices=[];
        for(const incoming of items){
            if(!incoming||typeof incoming!=="object") continue;

            const incomingId=String(incoming.id||"");
            let index=-1;

            if(incomingId){
                index=mediaData.findIndex(item=>String(item?.id||"")===incomingId);
            }

            if(index<0){
                const incomingUrl=String(incoming.url||"");
                index=mediaData.findIndex(item=>String(item?.url||"")===incomingUrl);
            }

            if(index>=0){
                mediaData[index]={
                    ...mediaData[index],
                    ...incoming,
                    metadata_obj:incoming.metadata_obj||incoming.metadata||{},
                    metadata:incoming.metadata||incoming.metadata_obj||{}
                };
            }else{
                mediaData.push({
                    ...incoming,
                    metadata_obj:incoming.metadata_obj||incoming.metadata||{},
                    metadata:incoming.metadata||incoming.metadata_obj||{}
                });
                index=mediaData.length-1;
            }

            indices.push(index);
        }
        return indices;
    }

    async function ensureVersionGallery(wantedName,wantedId){
        const versionId=String(wantedId||"").trim();
        const modelId=String(detail.dataset.modelId||"").trim();
        if(!versionId||!modelId) return matchingVersionIndices(wantedName,wantedId);

        if(versionMetadataRequests.has(versionId)){
            try{
                await versionMetadataRequests.get(versionId);
            }catch(e){}
            return matchingVersionIndices(wantedName,wantedId);
        }

        const request=(async()=>{
            try{
                const response=await fetch(
                    `/api/model/${encodeURIComponent(modelId)}/version/${encodeURIComponent(versionId)}/media-metadata`
                );
                const payload=await response.json().catch(()=>({}));
                if(response.ok&&Array.isArray(payload.media)){
                    mergeReturnedVersionMedia(payload.media);
                }
            }catch(e){
                // Keep the version selected even if Red is temporarily unavailable.
            }
        })();

        versionMetadataRequests.set(versionId,request);
        await request;
        return matchingVersionIndices(wantedName,wantedId);
    }

    async function applyVersionFilter(wantedName,wantedId,{hydrate=true}={}){
        wantedName=String(wantedName||"").trim().toLocaleLowerCase();
        wantedId=String(wantedId||"").trim();
        if(!wantedName&&!wantedId) return;

        const source=activeSourceView!=="combined"
            ? activeSourceView
            : String(detail.dataset.source||"").trim().toLowerCase();
        let matches=matchingVersionIndices(wantedName,wantedId);

        // Only sources whose media rows actually carry version identity should
        // have their gallery filtered by the version pills. CivitAI Red can
        // resolve a missing historical-version gallery on demand; other
        // sources must keep their normal full gallery instead of falling into
        // the Red-specific "Loading this version's previews..." state.
        if(!matches.length && source!=="civitaired"){
            visibleIndices=sourceVisibleIndices();
            total=visibleIndices.length;
            current=0;
            if(visibleIndices.length) showImage(0);
            else renderEmptyMedia("No previews found for this source.");
            return;
        }

        // CivitAI Red: never show another version's pictures while this version
        // is selected. If that version is not cached yet, resolve it lazily.
        visibleIndices=matches;
        total=visibleIndices.length;
        current=0;

        if(matches.length){
            showImage(0);
        }else{
            pauseCurrentVideo();
            if(virtualStage){
                const loading=document.createElement("div");
                loading.className="media-version-loading";
                loading.textContent=hydrate?"Loading this version's previews…":"No previews found for this version.";
                virtualStage.replaceChildren(loading);
                currentNode=loading;
            }
            if(counter) counter.textContent="0 / 0";
            if(prev) prev.style.display="none";
            if(next) next.style.display="none";
            if(filenameEl) filenameEl.textContent="";
            if(pathEl) pathEl.textContent="";
            if(metadataBody) metadataBody.innerHTML="";
        }

        if(!hydrate||!wantedId||source!=="civitaired") return;

        matches=await ensureVersionGallery(wantedName,wantedId);

        // The user may have selected another pill while the request was in flight.
        const selected=detail.querySelector(".model-version-pill.selected[data-version-name]");
        const stillSelectedId=String(selected?.dataset.versionId||"").trim();
        const stillSelectedName=String(selected?.dataset.versionName||"").trim().toLocaleLowerCase();
        if((wantedId&&stillSelectedId&&stillSelectedId!==wantedId) ||
           (!wantedId&&wantedName&&stillSelectedName&&stillSelectedName!==wantedName)){
            return;
        }

        visibleIndices=matches;
        total=matches.length;
        current=0;

        if(matches.length){
            showImage(0);
        }else{
            if(virtualStage){
                const empty=document.createElement("div");
                empty.className="media-version-loading";
                empty.textContent="No previews found for this version.";
                virtualStage.replaceChildren(empty);
                currentNode=empty;
            }
            if(counter) counter.textContent="0 / 0";
        }
    }

    detail.addEventListener("modelradar:version", event=>{
        applyVersionFilter(event.detail?.name,event.detail?.id);
    });

    async function applySourceFilter(source){
        const normalized=String(source||"combined").trim().toLowerCase()||"combined";
        activeSourceView=normalized;
        if(sourceViewSelect && sourceViewSelect.value!==normalized) sourceViewSelect.value=normalized;
        if(filesPanel) filesPanel.classList.remove("open");

        const selected=detail.querySelector(".model-version-pill.selected[data-version-name]");
        if(selected){
            await applyVersionFilter(
                selected.dataset.versionName||"",
                selected.dataset.versionId||""
            );
            return;
        }

        visibleIndices=sourceVisibleIndices();
        total=visibleIndices.length;
        current=0;
        if(total) showImage(0);
        else renderEmptyMedia();
    }

    detail.addEventListener("modelradar:source", event=>{
        applySourceFilter(event.detail?.source||"combined");
    });

    if(prev)prev.onclick=()=>showImage(current-1); if(next)next.onclick=()=>showImage(current+1);
    if(folderBtn)folderBtn.onclick=()=>renderFiles(actualIndex(),false); if(downloadModelBtn)downloadModelBtn.onclick=()=>renderFiles(actualIndex(),true); if(filesClose)filesClose.onclick=()=>filesPanel.classList.remove("open");

    // Treat the download/file chooser like a modal: clicking anywhere outside
    // the box closes it.  Use pointerdown so the gesture is handled before
    // the underlying model-detail controls can react to the same click.
    if(filesPanel){
        document.addEventListener("pointerdown", event=>{
            if(!filesPanel.classList.contains("open")) return;
            if(filesPanel.contains(event.target)) return;
            filesPanel.classList.remove("open");
        }, {signal:lifecycleSignal});
        document.addEventListener("keydown", event=>{
            if(event.key==="Escape" && filesPanel.classList.contains("open")) filesPanel.classList.remove("open");
        }, {signal:lifecycleSignal});
    }

    function setMetadataVisibility(hidden){if(!metadataCard)return;metadataCard.classList.toggle("user-hidden",hidden);if(metadataRestore)metadataRestore.classList.toggle("visible",hidden);try{localStorage.setItem("modelradarMetadataHidden",hidden?"1":"0");}catch(e){}}
    if(metadataToggle&&metadataCard)metadataToggle.onclick=()=>{const collapsed=metadataCard.classList.toggle("collapsed");metadataToggle.textContent=collapsed?"More":"Less";};
    if(metadataHide)metadataHide.onclick=()=>setMetadataVisibility(true); if(metadataRestore)metadataRestore.onclick=()=>setMetadataVisibility(false); try{setMetadataVisibility(localStorage.getItem("modelradarMetadataHidden")==="1");}catch(e){}

    function setExpanded(expanded){
        if(!panel)return; panel.classList.toggle("viewer-expanded",expanded); detail.classList.toggle("viewer-expanded",expanded); if(expandBtn)expandBtn.textContent=expanded?"Restore Viewer":"Expand Viewer";
        try{sessionStorage.setItem("modelradarViewerExpanded",expanded?"1":"0");}catch(e){}
    }
    if(expandBtn)expandBtn.onclick=()=>setExpanded(!panel.classList.contains("viewer-expanded"));
    try{if(sessionStorage.getItem("modelradarViewerExpanded")==="1")setExpanded(true);}catch(e){}

    const descriptionToggle=detail.querySelector(".description-toggle"), description=detail.querySelector(".model-description");
    if(descriptionToggle&&description){
        const updateDescriptionToggle=()=>{
            const collapsed=description.classList.contains("collapsed");
            descriptionToggle.textContent=collapsed?"Read more":"Show less";
        };
        // Only offer expansion when the collapsed container actually clips content.
        requestAnimationFrame(()=>{
            if(description.scrollHeight <= description.clientHeight + 2){
                description.classList.remove("collapsed");
                descriptionToggle.hidden=true;
            }
        });
        descriptionToggle.onclick=()=>{
            const collapsing=!description.classList.contains("collapsed");
            description.classList.toggle("collapsed", collapsing);
            updateDescriptionToggle();
            if(collapsing) description.scrollIntoView({behavior:"smooth", block:"start"});
        };
        updateDescriptionToggle();
    }

    // Pause playback whenever the media viewer is no longer meaningfully visible.
    // Returning to it does not auto-resume a video that was paused by scrolling.
    if(panel && container){
        let mediaWasVisible=true;
        const updateMediaPlaybackVisibility=()=>{
            if(!currentNode || currentNode.tagName!=="VIDEO") return;
            const panelRect=panel.getBoundingClientRect();
            const mediaRect=container.getBoundingClientRect();
            const visibleHeight=Math.max(0, Math.min(mediaRect.bottom,panelRect.bottom)-Math.max(mediaRect.top,panelRect.top));
            const visibleRatio=mediaRect.height>0?visibleHeight/mediaRect.height:0;
            const visible=visibleRatio>=0.20;
            if(mediaWasVisible && !visible) pauseCurrentVideo();
            mediaWasVisible=visible;
        };
        panel.addEventListener("scroll", updateMediaPlaybackVisibility, {passive:true, signal:lifecycleSignal});
    }

    // Opening the download workflow moves attention away from the preview.
    detail.addEventListener("click", event=>{
        if(event.target.closest(".detail-download-btn, .download-panel, .download-drawer, .download-close, .download-panel-close")){
            pauseCurrentVideo();
        }
    });

    const detailTop=detail.querySelector(".detail-back-to-top");
    const detailPanel=detail.closest(".model-panel");
    if(detailTop&&detailPanel){
        const updateDetailTop=()=>detailTop.classList.toggle("visible", detailPanel.scrollTop > 500);
        detailPanel.addEventListener("scroll", updateDetailTop, {passive:true, signal:lifecycleSignal});
        detailTop.onclick=()=>detailPanel.scrollTo({top:0, behavior:"smooth"});
        updateDetailTop();
    }

    function fullscreenStep(delta){
        if(total <= 1) return;
        showImage(current + delta);
        const activeIndex=actualIndex();
        const data=dataFor(activeIndex);
        if(String(data.type||"image").toLowerCase() !== "image") return;
        const image=document.getElementById("fullscreenImage");
        if(image){ image.src=data.url||""; resetImageZoom(); }
    }
    function openFullscreen(src){
        const overlay=document.getElementById("imageOverlay"),image=document.getElementById("fullscreenImage");
        if(!overlay||!image)return;
        overlay.classList.toggle("sensitive-blurred", detail.classList.contains("sensitive-blurred"));
        image.src=src;resetImageZoom();overlay.classList.add("open");
        const fp=document.getElementById("fullscreenPrev"),fn=document.getElementById("fullscreenNext");
        if(fp) fp.hidden=total<=1;
        if(fn) fn.hidden=total<=1;
    }
    const fullscreenPrev=document.getElementById("fullscreenPrev"), fullscreenNext=document.getElementById("fullscreenNext");
    if(fullscreenPrev) fullscreenPrev.addEventListener("click",e=>{e.stopPropagation();fullscreenStep(-1);},{signal:lifecycleSignal});
    if(fullscreenNext) fullscreenNext.addEventListener("click",e=>{e.stopPropagation();fullscreenStep(1);},{signal:lifecycleSignal});
    function updateImageTransform(){const image=document.getElementById("fullscreenImage");if(image)image.style.transform=`translate(${panX}px, ${panY}px) scale(${zoomLevel})`;}
    function resetImageZoom(){zoomLevel=1;panX=0;panY=0;updateImageTransform();}
    const fullscreenImage=document.getElementById("fullscreenImage");
    if(fullscreenImage){fullscreenImage.addEventListener("wheel",e=>{e.preventDefault();zoomLevel+=e.deltaY<0?.15:-.15;zoomLevel=Math.max(1,Math.min(5,zoomLevel));if(zoomLevel===1){panX=0;panY=0;}updateImageTransform();},{passive:false,signal:lifecycleSignal});fullscreenImage.addEventListener("mousedown",e=>{if(zoomLevel<=1)return;dragging=true;startX=e.clientX-panX;startY=e.clientY-panY;e.preventDefault();},{signal:lifecycleSignal});}
    document.addEventListener("mousemove",e=>{if(!dragging)return;panX=e.clientX-startX;panY=e.clientY-startY;updateImageTransform();},{signal:lifecycleSignal}); document.addEventListener("mouseup",()=>{dragging=false;},{signal:lifecycleSignal});

    document.addEventListener("keydown",e=>{
        const zoom=document.getElementById("imageOverlay");
        if(zoom&&zoom.classList.contains("open")){
            if(e.key==="ArrowLeft"){e.preventDefault();fullscreenStep(-1);return;}
            if(e.key==="ArrowRight"){e.preventDefault();fullscreenStep(1);return;}
        }
        if(e.key!=="Escape")return;
        if(zoom&&zoom.classList.contains("open")) return; // fullscreen.js handles first level
        if(panel&&panel.classList.contains("viewer-expanded")){e.stopImmediatePropagation();setExpanded(false);}
    },{capture:true,signal:lifecycleSignal});

    const initiallySelectedVersion=detail.querySelector(".model-version-pill.selected[data-version-name]");
    if(initiallySelectedVersion){
        applyVersionFilter(
            initiallySelectedVersion.dataset.versionName||"",
            initiallySelectedVersion.dataset.versionId||""
        );
    }else{
        showImage(0);
    }

    return ()=>{
        lifecycle.abort();
        dragging=false;
        detail.querySelectorAll("video").forEach(video=>{
            try{
                video.pause();
                video.removeAttribute("src");
                video.querySelectorAll("source").forEach(source=>source.removeAttribute("src"));
                video.load();
            }catch(e){}
        });
    };
}
