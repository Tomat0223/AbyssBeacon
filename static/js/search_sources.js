(function(){
  const $=id=>document.getElementById(id);
  const overlay=$('searchSourcesOverlay'), close=$('closeSourceSearch'), cancel=$('cancelSourceSearch');
  const query=$('sourceSearchQuery'), intent=$('sourceSearchIntent'), depth=$('sourceSearchDepth'), run=$('runSourceSearch'), status=$('sourceSearchStatus');
  const architectureAny=$('sourceSearchArchitectureAny');
  const intentHelp=$('sourceSearchIntentHelp'), sourceCount=$('sourceSearchSourceCount'), depthHelp=$('sourceSearchDepthHelp');
  let pollTimer=null, sawActive=false, pollStartedAt=0;

  function architectureInputs(){
    return Array.from(document.querySelectorAll('input[name="externalArchitecture"]'));
  }
  function selectAnyArchitecture(){
    if(architectureAny) architectureAny.checked=true;
    architectureInputs().forEach(input=>input.checked=false);
  }
  function selectedArchitectures(){
    if(architectureAny?.checked) return [];
    return architectureInputs().filter(input=>input.checked).map(input=>input.value);
  }
  architectureAny?.addEventListener('change',()=>{
    if(architectureAny.checked) architectureInputs().forEach(input=>input.checked=false);
    else if(!architectureInputs().some(input=>input.checked)) architectureAny.checked=true;
  });
  architectureInputs().forEach(input=>input.addEventListener('change',()=>{
    if(input.checked && architectureAny) architectureAny.checked=false;
    if(!architectureInputs().some(item=>item.checked) && architectureAny) architectureAny.checked=true;
  }));
  $('searchClearArchitectures')?.addEventListener('click',selectAnyArchitecture);
  $('searchAllArchitectures')?.addEventListener('click',()=>{
    if(architectureAny) architectureAny.checked=false;
    architectureInputs().forEach(input=>input.checked=true);
  });

  function hide(){ overlay?.classList.remove('open'); overlay?.setAttribute('aria-hidden','true'); }
  function show(){
    overlay?.classList.add('open');
    overlay?.setAttribute('aria-hidden','false');
    if(status) status.textContent='';
    setTimeout(()=>query?.focus(),30);
  }
  window.openSourceSearch=show;

  function updateSourceCount(){
    const all=Array.from(document.querySelectorAll('input[name="externalSource"]'));
    const selected=all.filter(input=>input.checked).length;
    if(sourceCount) sourceCount.textContent=`${selected}/${all.length} selected`;
  }
  function setSources(checked){
    document.querySelectorAll('input[name="externalSource"]').forEach(input=>input.checked=checked);
    updateSourceCount();
  }
  $('searchAllSources')?.addEventListener('click',()=>setSources(true));
  $('searchClearSources')?.addEventListener('click',()=>setSources(false));
  document.querySelectorAll('input[name="externalSource"]').forEach(input=>input.addEventListener('change',updateSourceCount));

  function updateIntentHelp(){
    const mode=String(intent?.value||'models');
    if(!intentHelp) return;
    if(mode==='anything') intentHelp.textContent='Searches both models and creators. On sources with creator catalogs, matching creators can contribute models they published.';
    else if(mode==='creators') intentHelp.textContent='Finds matching creator accounts and imports models from their creator catalogs on supported sources.';
    else intentHelp.textContent='Searches model names and metadata for the keyword.';
  }
  function updateDepthHelp(){
    if(!depthHelp) return;
    const mode=String(depth?.value||'recent');
    depthHelp.textContent=mode==='maximum'
      ? 'No practical AbyssBeacon result cap is applied. Each provider is searched toward its available end; this can take a long time.'
      : 'Depth controls how many matches each provider may inspect. Search is all-time at every depth; providers run in parallel.';
  }
  intent?.addEventListener('change',updateIntentHelp);
  depth?.addEventListener('change',updateDepthHelp);
  updateIntentHelp(); updateDepthHelp(); updateSourceCount();

  function stopPolling(){ if(pollTimer){ clearTimeout(pollTimer); pollTimer=null; } }
  function poll(){
    stopPolling();
    sawActive=false;
    pollStartedAt=Date.now();
    const tick=async()=>{
      try{
        const r=await fetch('/scan/status',{cache:'no-store'}); const data=await r.json();
        const state=String(data.status||'');
        if(state==='running' || state==='stopping') sawActive=true;

        // /scan/status may still contain a terminal state from the previous
        // scan while the new background search thread is starting. Never
        // refresh until this polling session has actually observed running.
        if(!sawActive){
          status.textContent='Starting source search…';
        } else if(['complete','complete_with_errors','stopped','error','idle'].includes(state)){
          run.disabled=false;
          if(state==='complete') status.textContent=`Search complete — ${Number(data.added||0)} new, ${Number(data.updated||0)} updated. Refreshing…`;
          else if(state==='complete_with_errors') status.textContent=`Search complete with source errors — ${Number(data.added||0)} new, ${Number(data.updated||0)} updated. Refreshing…`;
          else if(state==='stopped') status.textContent='Search stopped.';
          else if(state==='error') status.textContent=data.message||'Search failed.';
          else status.textContent='Search complete. Refreshing…';
          stopPolling();
          if(state==='complete' || state==='complete_with_errors' || (state==='idle' && sawActive)){
            setTimeout(()=>window.location.reload(),650);
          }
          return;
        } else {
          status.textContent=data.message || 'Searching sources… results will be imported into AbyssBeacon.';
        }
      }catch(_e){}
      pollTimer=setTimeout(tick,700);
    };
    pollTimer=setTimeout(tick,250);
  }

  close?.addEventListener('click',hide); cancel?.addEventListener('click',hide);
  overlay?.addEventListener('click',e=>{if(e.target===overlay)hide();});
  query?.addEventListener('keydown',event=>{ if(event.key==='Enter'){ event.preventDefault(); run?.click(); } });

  run?.addEventListener('click',async()=>{
    const sources=Array.from(document.querySelectorAll('input[name="externalSource"]:checked')).map(x=>x.value);
    const architectures=selectedArchitectures();
    if(!query?.value.trim()){status.textContent='Enter something to search for.';return;}
    if(!sources.length){status.textContent='Select at least one source.';return;}
    run.disabled=true; status.textContent=depth.value==='maximum'?'Searching sources to provider end… this can take a while.':'Starting source search…';
    try{
      const r=await fetch('/search/sources',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:query.value.trim(),intent:intent.value,architectures,depth:depth.value,sources})});
      const data=await r.json(); if(!r.ok) throw new Error(data.message||'Search could not start.');
      status.textContent='Searching sources… results will be imported into AbyssBeacon.'; poll();
    }catch(e){run.disabled=false; status.textContent=e.message;}
  });
})();
