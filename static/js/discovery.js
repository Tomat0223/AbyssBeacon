(function(){
    const $ = id => document.getElementById(id);
    const overlay = $('discoveryScanOverlay');
    const openButton = $('discoveryScanButton');
    const closeButton = $('closeDiscoveryScan');
    const cancelButton = $('cancelDiscoveryScan');
    const runButton = $('runDiscoveryScan');
    const source = $('discoverySource');
    const type = $('discoveryType');
    const tagInput = $('discoveryTag');
    const tagId = $('discoveryTagId');
    const suggestions = $('discoveryTagSuggestions');
    const sort = $('discoverySort');
    const maxResults = $('discoveryMaxResults');
    const watchOnly = $('discoveryWatchOnly');
    const status = $('discoveryScanStatus');
    const tagHelp = $('discoveryTagHelp');
    const sortHelp = $('discoverySortHelp');
    const tagBankButton = $('openDiscoveryTagBank');
    const tagBank = $('discoveryTagBank');
    const tagBankTitle = $('discoveryTagBankTitle');
    const tagBankClose = $('closeDiscoveryTagBank');
    const tagBankSearch = $('discoveryTagBankSearch');
    const tagBankBody = $('discoveryTagBankBody');

    let tagBankItems = [];
    let suggestionTimer = null;
    let suggestionAbort = null;
    let activeSuggestionIndex = -1;

    function hideSuggestions(){
        suggestions?.classList.add('hidden');
        if(suggestions) suggestions.innerHTML = '';
        activeSuggestionIndex = -1;
    }

    function hide(){
        overlay?.classList.remove('open');
        overlay?.setAttribute('aria-hidden','true');
        hideSuggestions();
    }

    function show(){
        overlay?.classList.add('open');
        overlay?.setAttribute('aria-hidden','false');
        if(status) status.textContent = '';
        setTimeout(() => tagInput?.focus(), 40);
    }
    window.openDiscoveryScan = show;

    function renderSuggestions(items){
        if(!suggestions) return;
        suggestions.innerHTML = '';
        if(!items?.length){
            suggestions.classList.add('hidden');
            return;
        }
        items.forEach((item, index) => {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'discovery-tag-option';
            button.dataset.tagId = item.id;
            button.dataset.tagName = item.name;
            button.dataset.suggestionIndex = String(index);
            const note = source?.value === 'modelscope' && item.type ? item.type : `${Number(item.count||0)} stored match${Number(item.count||0)===1?'':'es'}`;
            button.innerHTML = `<span>${escapeHtml(item.name)}</span><small>${escapeHtml(note)}</small>`;
            button.addEventListener('mouseenter', () => setActiveSuggestion(index));
            button.addEventListener('mousedown', event => {
                event.preventDefault();
                chooseSuggestion(button);
            });
            suggestions.appendChild(button);
        });
        activeSuggestionIndex = items.length ? 0 : -1;
        updateActiveSuggestion();
        suggestions.classList.remove('hidden');
    }

    function suggestionButtons(){
        return Array.from(suggestions?.querySelectorAll('.discovery-tag-option') || []);
    }

    function updateActiveSuggestion(){
        const buttons = suggestionButtons();
        buttons.forEach((button, index) => button.classList.toggle('active', index === activeSuggestionIndex));
        if(activeSuggestionIndex >= 0 && buttons[activeSuggestionIndex]){
            buttons[activeSuggestionIndex].scrollIntoView({block:'nearest'});
        }
    }

    function setActiveSuggestion(index){
        const buttons = suggestionButtons();
        if(!buttons.length){
            activeSuggestionIndex = -1;
            return;
        }
        activeSuggestionIndex = Math.max(0, Math.min(buttons.length - 1, index));
        updateActiveSuggestion();
    }

    function chooseSuggestion(button){
        if(!button) return false;
        if(tagInput) tagInput.value = button.dataset.tagName || '';
        if(tagId) tagId.value = button.dataset.tagId || '';
        hideSuggestions();
        return true;
    }

    function chooseActiveSuggestion(){
        const buttons = suggestionButtons();
        if(!buttons.length) return false;
        const index = activeSuggestionIndex >= 0 ? activeSuggestionIndex : 0;
        return chooseSuggestion(buttons[index] || buttons[0]);
    }

    function escapeHtml(value){
        return String(value ?? '')
            .replaceAll('&','&amp;')
            .replaceAll('<','&lt;')
            .replaceAll('>','&gt;')
            .replaceAll('"','&quot;')
            .replaceAll("'",'&#39;');
    }

    async function lookupTags(){
        const q = String(tagInput?.value || '').trim();
        if(q.length < 2){
            hideSuggestions();
            return;
        }
        if(/^\d{8,}$/.test(q) || /tensorhub\.art\/(?:models\/)?tag\/\d+/i.test(q)){
            hideSuggestions();
            return;
        }
        suggestionAbort?.abort();
        suggestionAbort = new AbortController();
        try{
            const response = await fetch(`/discover/tags?source=${encodeURIComponent(source.value)}&q=${encodeURIComponent(q)}`, {
                cache:'no-store',
                signal:suggestionAbort.signal,
            });
            const data = await response.json();
            renderSuggestions(data.tags || []);
        }catch(error){
            if(error.name !== 'AbortError') hideSuggestions();
        }
    }

    function scheduleLookup(){
        if(tagId) tagId.value = '';
        clearTimeout(suggestionTimer);
        suggestionTimer = setTimeout(lookupTags, 180);
    }

    function selectBankTag(item){
        if(tagInput) tagInput.value = item.name || item.slug || item.id || '';
        if(tagId) tagId.value = item.slug || item.id || '';
        tagBank?.classList.add('hidden');
        hideSuggestions();
        tagInput?.focus();
    }

    function renderTagBank(){
        if(!tagBankBody) return;
        const q = String(tagBankSearch?.value || '').trim().toLowerCase();
        const filtered = tagBankItems.filter(item => !q || [item.name,item.slug,item.id,item.type].some(v => String(v||'').toLowerCase().includes(q)));
        tagBankBody.innerHTML = '';
        const groups = new Map();
        filtered.forEach(item => {
            const key = item.type || 'Tags';
            if(!groups.has(key)) groups.set(key, []);
            groups.get(key).push(item);
        });
        groups.forEach((items, group) => {
            const section = document.createElement('section');
            const heading = document.createElement('strong');
            heading.className = 'discovery-tag-bank-group-title';
            heading.textContent = String(group).replace(/([a-z])([A-Z])/g, '$1 $2');
            section.appendChild(heading);
            const grid = document.createElement('div');
            grid.className = 'discovery-tag-bank-grid';
            items.forEach(item => {
                const button = document.createElement('button');
                button.type = 'button';
                button.textContent = item.name || item.slug || item.id;
                button.title = item.slug && item.slug !== item.name ? item.slug : '';
                button.addEventListener('click', () => selectBankTag(item));
                grid.appendChild(button);
            });
            section.appendChild(grid);
            tagBankBody.appendChild(section);
        });
        if(!filtered.length) tagBankBody.textContent = 'No matching tags.';
    }

    async function openTagBank(){
        const current = source?.value || '';
        if(!current || !tagBank) return;
        tagBank.classList.remove('hidden');
        if(tagBankTitle) tagBankTitle.textContent = `${source?.selectedOptions?.[0]?.textContent || current} Tags`;
        if(tagBankBody) tagBankBody.textContent = 'Loading tags…';
        try{
            const response = await fetch(`/discover/tag-bank?source=${encodeURIComponent(current)}`, {cache:'no-store'});
            const raw = await response.text();
            let data = {};
            try{
                data = raw ? JSON.parse(raw) : {};
            }catch(_error){
                throw new Error(response.ok
                    ? 'The tag catalog returned an invalid response.'
                    : `Could not load tags (HTTP ${response.status}).`);
            }
            if(!response.ok || !data.success) throw new Error(data.error || 'Could not load tags.');
            tagBankItems = Array.isArray(data.tags) ? data.tags : [];
            renderTagBank();
            tagBankSearch?.focus();
        }catch(error){ if(tagBankBody) tagBankBody.textContent = error.message; }
    }

    tagBankButton?.addEventListener('click', openTagBank);
    tagBankClose?.addEventListener('click', () => tagBank?.classList.add('hidden'));
    tagBankSearch?.addEventListener('input', renderTagBank);

    openButton?.addEventListener('click', show);
    closeButton?.addEventListener('click', hide);
    cancelButton?.addEventListener('click', hide);
    overlay?.addEventListener('click', event => { if(event.target === overlay) hide(); });
    tagInput?.addEventListener('input', scheduleLookup);
    tagInput?.addEventListener('focus', scheduleLookup);
    tagInput?.addEventListener('blur', () => setTimeout(hideSuggestions, 120));
    tagInput?.addEventListener('keydown', event => {
        const buttons = suggestionButtons();
        const open = !!buttons.length && !suggestions?.classList.contains('hidden');
        if((event.key === 'Tab' || event.key === 'Enter') && open){
            if(chooseActiveSuggestion()){
                event.preventDefault();
            }
            return;
        }
        if(event.key === 'ArrowDown' && open){
            event.preventDefault();
            setActiveSuggestion((activeSuggestionIndex + 1) % buttons.length);
            return;
        }
        if(event.key === 'ArrowUp' && open){
            event.preventDefault();
            setActiveSuggestion((activeSuggestionIndex - 1 + buttons.length) % buttons.length);
            return;
        }
        if(event.key === 'Escape' && open){
            event.preventDefault();
            hideSuggestions();
        }
    });
    function updateSourceUI(){
        if(tagId) tagId.value = '';
        hideSuggestions();
        const current = source?.value || '';
        if(tagBankButton) tagBankButton.classList.toggle('hidden', !['modelscope','tensorhub','seaart','civitai','civitaired'].includes(current));
        tagBank?.classList.add('hidden');
        if(sortHelp) sortHelp.textContent = current === 'modelscope' ? 'ModelScope official-tag results currently use the source’s default ordering.' : 'Newest is the default because Discovery Scan is designed to surface newly published models.';
        if(current === 'civitaired' || current === 'civitai'){
            if(tagInput) tagInput.placeholder = 'Try horror, movie, style…';
            if(tagHelp) tagHelp.textContent = current === 'civitaired' ? 'Enter the CivitAI Red tag name exactly as it appears on the site. Discovery uses your saved Red session.' : 'Enter a CivitAI tag name. Discovery uses CivitAI’s public model API and defaults to newest.';
            if(sort){
                sort.innerHTML = current === 'civitai' ? '<option value="NEWEST">Newest</option><option value="HIGHEST_RATED">Highest Rated</option><option value="MOST_DOWNLOADED">Most Downloaded</option>' : '<option value="NEWEST">Newest</option><option value="HIGHEST_RATED">Highest Rated</option>';
                sort.value = 'NEWEST';
            }
        }else if(current === 'seaart'){
            if(tagInput) tagInput.placeholder = 'Try character, style, photography…';
            if(tagHelp) tagHelp.textContent = 'Enter a SeaArt tag/category. AbyssBeacon uses SeaArt’s tag model-list endpoint and requests New ordering.';
            if(sort){
                sort.innerHTML = '<option value="NEWEST">Newest</option><option value="HIGHEST_RATED">Hot</option>';
                sort.value = 'NEWEST';
            }
        }else if(current === 'modelscope'){
            if(tagInput) tagInput.placeholder = 'Try character-enhancement, photography, woman…';
            if(tagHelp) tagHelp.textContent = 'Start typing and choose a ModelScope official tag. Tab/Enter accepts the highlighted suggestion; AbyssBeacon sends ModelScope’s required tag slug automatically.';
            if(sort){
                sort.innerHTML = '<option value="NEWEST">Source default</option>';
                sort.value = 'NEWEST';
            }
        }else{
            if(tagInput) tagInput.placeholder = 'Try photorealistic… or paste a TensorHub tag URL';
            if(tagHelp) tagHelp.textContent = 'Type to search tags AbyssBeacon has already seen. You can also paste a TensorHub tag URL or numeric tag ID.';
            if(sort){
                sort.innerHTML = '<option value="NEWEST">Newest</option><option value="LATEST_UPDATE">Latest Updated</option><option value="HOT_TODAY">Hot Today</option>';
                sort.value = 'NEWEST';
            }
        }
    }
    source?.addEventListener('change', updateSourceUI);
    updateSourceUI();

    runButton?.addEventListener('click', async () => {
        const rawTag = String(tagInput?.value || '').trim();
        if(!source?.value){
            if(status) status.textContent = 'Enable a Discovery-capable source first.';
            return;
        }
        if(!rawTag && !tagId?.value){
            if(status) status.textContent = source?.value === 'tensorhub' ? 'Choose a tag or paste a TensorHub tag URL/ID.' : 'Enter a tag/category name.';
            tagInput?.focus();
            return;
        }
        let limit = Number(maxResults?.value || 100);
        if(!Number.isFinite(limit)) limit = 100;
        limit = Math.max(1, Math.min(5000, Math.trunc(limit)));
        if(maxResults) maxResults.value = String(limit);

        runButton.disabled = true;
        if(openButton) openButton.disabled = true;
        if(status) status.textContent = 'Starting Discovery Scan…';

        try{
            const response = await fetch('/discover/scan', {
                method:'POST',
                headers:{'Content-Type':'application/json'},
                body:JSON.stringify({
                    source:source.value,
                    type:type?.value || 'tag',
                    tag:rawTag,
                    tag_id:String(tagId?.value || '').trim(),
                    tag_name:tagId?.value ? rawTag : '',
                    sort:sort?.value || 'NEWEST',
                    max_results:limit,
                    watch_only:watchOnly?.checked !== false,
                }),
            });
            const data = await response.json();
            if(!response.ok || !data.success) throw new Error(data.error || 'Discovery Scan could not start.');

            hide();
            if(typeof scanRunning !== 'undefined') scanRunning = true;
            if(typeof setScanButtonRunning === 'function') setScanButtonRunning(true);
            if(typeof showScanProgress === 'function') showScanProgress();
            if(typeof renderScanStatus === 'function') renderScanStatus({
                status:'running', source:data.source, message:(data.source === 'modelscope' ? `Scanning ModelScope tag: ${data.tag_name || rawTag}…` : `Discovery Scan: ${data.tag_name || rawTag}`),
                processed:0, added:0, updated:0, images:0, videos:0,
            });
            if(typeof watchScan === 'function') watchScan();
        }catch(error){
            runButton.disabled = false;
            if(openButton) openButton.disabled = false;
            if(status) status.textContent = error.message;
        }
    });

    // scanner.js reloads the page after successful scans, so this mainly handles
    // a failed/cancelled start without leaving the Discover button disabled.
    document.addEventListener('modelradar:scan-visibility', () => {
        if(typeof scanRunning !== 'undefined' && !scanRunning){
            if(runButton) runButton.disabled = false;
            if(openButton) openButton.disabled = false;
        }
    });
})();
