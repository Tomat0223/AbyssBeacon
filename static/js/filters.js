const preferenceElement = document.getElementById("user-preferences");

window.userPreferences = preferenceElement
    ? JSON.parse(preferenceElement.textContent)
    : {};

let committedSearchTerms = [];
let preferenceSaveTimer = null;
let dismissedAutocompleteValue = null;
let autocompleteSelection = -1;
let navbarCountTimer = null;
let navbarCountRequestId = 0;
let lastNavbarCountSignature = "";
let lastNavbarCountData = null;
let favoriteCreatorNames = new Set();
try {
    favoriteCreatorNames = new Set(JSON.parse(document.getElementById("favorite-creators-data")?.textContent || "[]").map(name => String(name).toLowerCase()));
} catch (_) {}
window.favoriteCreatorNames = favoriteCreatorNames;
const SEARCH_COMMANDS = ["discover", "search", "exclude:", "author:", "arch:", "type:", "source:", "tag:", "sha:", "status:", "access:", "downloaded:", "update:", "mature:", "media:", "favorite:", "favorite-creator:"];
const SEARCH_VALUE_SUGGESTIONS = {
    "favorite:": [
        ["favorite:true", "Favorite models"],
        ["favorite:false", "Non-favorite models"]
    ],
    "favorite-creator:": [
        ["favorite-creator:true", "Models by favorite creators"],
        ["favorite-creator:false", "Models by other creators"]
    ],
    "mature:": [["mature:true", "Mature models"], ["mature:false", "Non-mature models"]],
    "media:": [["media:true", "Models with media"], ["media:false", "Models without media"]],
    "access:": [["access:downloadable", "Downloadable models"], ["access:paid_access", "Paid access models"], ["access:unknown", "Unknown download status"], ["access:gated", "Gated / no-download models"]],
    "status:": [["status:new", "New models"], ["status:seen", "Seen models"], ["status:updated", "Updated in latest scan"]],
    "downloaded:": [["downloaded:true", "Previously downloaded"], ["downloaded:false", "Never downloaded"]],
    "update:": [["update:true", "Downloaded models with updates"], ["update:false", "No tracked update"]],
    "exclude:": [["exclude:gated", "Hide gated models"], ["exclude:unknown", "Hide unknown download status"], ["exclude:downloadable", "Hide downloadable models"]]
};


function ensureSearchUI(){
    if(document.getElementById("modelSearch")) return;

    const navbar=document.querySelector(".navbar");
    const cluster=navbar?.querySelector(".nav-center-cluster");
    if(!navbar || !cluster) return;

    const area=document.createElement("div");
    area.className="nav-search-area";
    area.innerHTML=`
        <div class="nav-search-box">
            <span class="nav-search-icon" aria-hidden="true">⌕</span>
            <div id="searchAutocompleteGhost" class="search-autocomplete-ghost" aria-hidden="true"><span class="autocomplete-prefix"></span><span class="autocomplete-suffix"></span></div>
            <input id="modelSearch" type="search" autocomplete="off" spellcheck="false"
                   placeholder="Search AbyssBeacon…"
                   aria-label="Search and filter models">
            <button id="clearAllSearchFilters" class="search-clear-all hidden" type="button"
                    aria-label="Clear search and all filters" title="Clear search and all filters">×</button>
        </div>
        <div id="searchAutocompleteMenu" class="search-autocomplete-menu" role="listbox" aria-label="Search suggestions"></div>
        <div id="activeFilterPills" class="active-filter-pills" aria-label="Active filters"></div>
        <div class="search-help-tooltip" role="tooltip">
            Type anything and press Enter to pin it as a filter.<br>
            Try <strong>search</strong> to find models or creators not currently in AbyssBeacon, or <strong>discover</strong> to find models by tags not currently in AbyssBeacon.<br>
            Precise filters: <strong>exclude:</strong> <strong>author:</strong> <strong>arch:</strong> <strong>type:</strong> <strong>source:</strong> <strong>tag:</strong> <strong>sha:</strong> <strong>status:</strong> <strong>access:</strong> <strong>mature:</strong> <strong>media:</strong> <strong>downloaded:</strong> <strong>update:</strong> <strong>favorite:</strong>
        </div>`;

    navbar.insertBefore(area, cluster);
}

function initializeFilters(){

    ensureSearchUI();
    restorePreferences();

    document.querySelectorAll('input[name="sources"]').forEach(input => {
        input.addEventListener("change", async () => {
            updateSourcePills();
            updateSourceFilterSummary();
            savePreferences();
            if(typeof window.modelRadarResetFeedWindow === "function") {
                await window.modelRadarResetFeedWindow();
            } else {
                filterCards();
            }
        });
    });

    const setAllSources=async (checked)=>{
        document.querySelectorAll('input[name="sources"]').forEach(input => input.checked=checked);
        updateSourcePills();
        updateSourceFilterSummary();
        savePreferences();
        if(typeof window.modelRadarResetFeedWindow === "function") {
            await window.modelRadarResetFeedWindow();
        } else {
            filterCards();
        }
    };
    document.getElementById("selectAllSources")?.addEventListener("click", event=>{event.preventDefault();setAllSources(true);});
    document.getElementById("clearSources")?.addEventListener("click", event=>{event.preventDefault();setAllSources(false);});
    document.querySelector(".clear-sources")?.addEventListener("click", event=>{event.stopPropagation();setAllSources(false);});

    ["familyFilter", "modelTypeFilter", "statusFilter", "accessFilter", "sensitiveFilter", "favoriteFilter", "creatorFavoriteFilter", "downloadStatusFilter"].forEach(id => {
        document.getElementById(id)?.addEventListener("change", async () => {
            savePreferences();

            // Structural filters must be applied by SQLite before LIMIT/OFFSET.
            // Replace the feed first, then let feed_window.js apply the local-only
            // presentation filters to that first 120-card result window. Doing a
            // client hide/show pass first can leave the bottom sentinel exposed
            // and make it race through every matching chunk.
            const windowed = [
                "familyFilter",
                "modelTypeFilter",
                "statusFilter",
                "sensitiveFilter",
                "favoriteFilter",
                "creatorFavoriteFilter",
                "downloadStatusFilter"
            ].includes(id);

            if(windowed && typeof window.modelRadarResetFeedWindow === "function") {
                await window.modelRadarResetFeedWindow();
            } else {
                filterCards();
            }
        });
    });

    const showMediaOnly = document.getElementById("showMediaOnly");
    showMediaOnly?.addEventListener("change", async () => {
        savePreferences();
        if(typeof window.modelRadarResetFeedWindow === "function") {
            await window.modelRadarResetFeedWindow();
        } else {
            filterCards();
        }
    });

    const modelSearch = document.getElementById("modelSearch");
    let searchFeedResetTimer = null;

    function scheduleSearchFeedReset(){
        clearTimeout(searchFeedResetTimer);
        searchFeedResetTimer = setTimeout(() => {
            window.modelRadarResetFeedWindow?.({reason:"search"});
        }, 220);
    }

    modelSearch?.addEventListener("input", () => {
        dismissedAutocompleteValue = null;
        autocompleteSelection = -1;
        updateSearchAutocomplete();
        filterCards();
        savePreferences();
        scheduleSearchFeedReset();
    });
    modelSearch?.addEventListener("keydown", event => {
        const suggestions = getSearchAutocompleteSuggestions();

        if(event.key === "ArrowDown" && suggestions.length) {
            event.preventDefault();
            autocompleteSelection = (autocompleteSelection + 1) % suggestions.length;
            updateSearchAutocomplete();
            return;
        }
        if(event.key === "ArrowUp" && suggestions.length) {
            event.preventDefault();
            autocompleteSelection = autocompleteSelection <= 0 ? suggestions.length - 1 : autocompleteSelection - 1;
            updateSearchAutocomplete();
            return;
        }
        if((event.key === "Tab" || event.key === "ArrowRight") && suggestions.length) {
            event.preventDefault();
            acceptSearchAutocomplete(autocompleteSelection >= 0 ? autocompleteSelection : 0);
            return;
        }
        if(event.key === "Enter") {
            event.preventDefault();
            // Enter only accepts autocomplete after the user explicitly selected
            // a row with the arrow keys. Otherwise literal text is committed.
            if(autocompleteSelection >= 0 && suggestions[autocompleteSelection]) {
                acceptSearchAutocomplete(autocompleteSelection);
                return;
            }
            const term = modelSearch.value.trim();
            if(term.toLowerCase() === "discover" || term.toLowerCase() === "search") {
                const command = term.toLowerCase();
                modelSearch.value = "";
                dismissedAutocompleteValue = null;
                autocompleteSelection = -1;
                updateSearchAutocomplete();
                filterCards();
                savePreferences();
                restoreFeedAfterSearchCommand(command);
                if(command === "discover") window.openDiscoveryScan?.();
                else window.openSourceSearch?.();
                return;
            }
            if(term) {
                const reflectedInOptions = applyStructuredSearchToOptions(term);
                if(!reflectedInOptions) committedSearchTerms.push(term);
                modelSearch.value = "";
                autocompleteSelection = -1;
                updateSearchAutocomplete();
                filterCards();
                savePreferences();
                window.modelRadarResetFeedWindow?.({reason:"search-commit"});
            }
            return;
        }
        if(event.key === "Escape") {
            if(suggestions.length) {
                dismissedAutocompleteValue = modelSearch.value;
                autocompleteSelection = -1;
                updateSearchAutocomplete();
                return;
            }
            modelSearch.value = "";
            filterCards();
            savePreferences();
            window.modelRadarResetFeedWindow?.({reason:"search-clear"});
            modelSearch.blur();
        }
    });
    document.getElementById("clearAllSearchFilters")?.addEventListener("click", resetFilters);

    document.getElementById("resetFilters")?.addEventListener("click", resetFilters);
    document.getElementById("emptyResetFilters")?.addEventListener("click", resetFilters);

    function applyNavbarStatusShortcut(value){
        const status = document.getElementById("statusFilter");
        if(status){
            status.value = value;

            // Current AbyssBeacon uses a native Status select in the Options
            // panel. Dispatching change keeps its visible selection and any
            // regular filter listeners synchronized with navbar shortcuts.
            status.dispatchEvent(new Event("change", {bubbles:true}));

            // Keep compatibility with the older custom Status component if it
            // is reused on another page.
            const wrapper = status.closest(".status-filter");
            const label = wrapper?.querySelector(".filter-button span:first-child");
            if(label){
                label.textContent = String(value).toLowerCase() === "all"
                    ? "Status"
                    : value;
            }
            wrapper?.querySelectorAll(".filter-option").forEach(option => {
                option.classList.toggle(
                    "active",
                    String(option.dataset.value || "").toLowerCase()
                        === String(value || "").toLowerCase()
                );
            });
        }

        committedSearchTerms = committedSearchTerms.filter(
            term => !/^status\s*:/i.test(String(term || "").trim())
        );

        filterCards();
        savePreferences();
    }

    document.getElementById("newCount")?.addEventListener(
        "click",
        () => applyNavbarStatusShortcut("New")
    );
    document.getElementById("updatedCount")?.addEventListener(
        "click",
        () => applyNavbarStatusShortcut("Updated")
    );
    document.getElementById("modelCount")?.addEventListener(
        "click",
        () => applyNavbarStatusShortcut("All")
    );

    document.getElementById("markVisibleSeen")?.addEventListener("click", () => requestMarkSeen("visible"));
    document.getElementById("markAllSeen")?.addEventListener("click", () => requestMarkSeen("all"));

    updateSourcePills();
    updateSourceFilterSummary();
    updateSearchAutocomplete();
    filterCards();
    document.body.classList.remove("loading");
}

function getDynamicSearchValues(command){
    if(command === "source:") {
        return Array.from(document.querySelectorAll('input[name="sources"]')).map(input=>[
            `source:${String(input.value).toLowerCase().replace(/\s+/g, "")}`,
            input.dataset.sourceLabel || input.closest("label")?.innerText.trim() || input.value
        ]);
    }
    if(command === "arch:") {
        return Array.from(document.querySelectorAll("#familyFilter option"))
            .filter(option=>option.value)
            .map(option=>[`arch:${option.value}`, option.textContent.trim()]);
    }
    if(command === "type:") {
        return Array.from(document.querySelectorAll("#modelTypeFilter option"))
            .filter(option=>option.value)
            .map(option=>[`type:${option.value}`, option.textContent.trim()]);
    }
    return SEARCH_VALUE_SUGGESTIONS[command] || [];
}

function getSearchAutocompleteSuggestions(){
    const input = document.getElementById("modelSearch");
    const raw = input?.value || "";
    const value = raw.trim().toLowerCase();
    if(!value || raw !== raw.trim() || value.includes(" ")) return [];
    if(dismissedAutocompleteValue === raw) return [];

    const matches=[];
    for(const command of SEARCH_COMMANDS) {
        if(!(command.startsWith(value) || value.startsWith(command))) continue;
        const valueOptions=getDynamicSearchValues(command);
        // Once a command is being typed ("sou") or has been completed
        // ("source:"), show useful values as well as teaching the syntax.
        if(valueOptions.length && (command.startsWith(value) || value.startsWith(command))) {
            valueOptions.forEach(([text,label])=>{
                if(text.toLowerCase().startsWith(value) || command.startsWith(value)) {
                    matches.push({text,label:`${command.slice(0,-1)} · ${label}`});
                }
            });
        }
        if(command.startsWith(value) && command !== value) {
            const label = command === "discover" ? "Open Discovery Scan" : (command === "search" ? "Open Search Sources" : "Search filter");
            matches.push({text:command,label});
        }
    }
    const unique=[]; const seen=new Set();
    for(const item of matches){if(!seen.has(item.text.toLowerCase())){seen.add(item.text.toLowerCase());unique.push(item);}}
    return unique.slice(0, 10);
}

function getSearchAutocompleteSuggestion(){
    return getSearchAutocompleteSuggestions()[0]?.text || null;
}

function restoreFeedAfterSearchCommand(command){
    // Typing "search" / "discover" is temporarily a literal feed search. If
    // feed windowing already replaced the grid with those literal results,
    // clearing the command must explicitly rebuild the normal feed window.
    if(typeof window.modelRadarResetFeedWindow === "function"){
        Promise.resolve(
            window.modelRadarResetFeedWindow({reason:`search-command:${command}`})
        ).catch(error => console.error("Unable to restore AbyssBeacon feed after search command:", error));
    }else{
        filterCards();
    }
}

function acceptSearchAutocomplete(index=0){
    const input=document.getElementById("modelSearch");
    const suggestions=getSearchAutocompleteSuggestions();
    const choice=suggestions[index];
    if(!input || !choice) return;
    if(["discover", "search"].includes(choice.text.toLowerCase())) {
        const command = choice.text.toLowerCase();
        input.value = "";
        dismissedAutocompleteValue = null;
        autocompleteSelection = -1;
        updateSearchAutocomplete();
        filterCards();
        savePreferences();
        restoreFeedAfterSearchCommand(command);
        if(command === "discover") window.openDiscoveryScan?.();
        else window.openSourceSearch?.();
        return;
    }
    input.value=choice.text;
    dismissedAutocompleteValue=null;
    autocompleteSelection=-1;
    updateSearchAutocomplete();
    filterCards();
    input.focus();
}

function updateSearchAutocomplete(){
    const input = document.getElementById("modelSearch");
    const ghost = document.getElementById("searchAutocompleteGhost");
    let menu = document.getElementById("searchAutocompleteMenu");
    const area = input?.closest(".nav-search-area");
    if(!input || !ghost) return;

    if(!menu && area) {
        menu=document.createElement("div");
        menu.id="searchAutocompleteMenu";
        menu.className="search-autocomplete-menu";
        menu.setAttribute("role","listbox");
        menu.setAttribute("aria-label","Search suggestions");
        area.insertBefore(menu, document.getElementById("activeFilterPills"));
    }

    const suggestions = getSearchAutocompleteSuggestions();
    if(autocompleteSelection >= suggestions.length) autocompleteSelection=-1;
    const prefix = ghost.querySelector(".autocomplete-prefix");
    const suffix = ghost.querySelector(".autocomplete-suffix");

    // Keep the original inline ghost for a single simple command suggestion.
    const simpleGhost = suggestions.length === 1 && suggestions[0].label === "Search filter";
    if(simpleGhost) {
        const typed=input.value;
        if(prefix) prefix.textContent=typed;
        if(suffix) suffix.textContent=suggestions[0].text.slice(typed.length);
        ghost.classList.add("visible");
    } else {
        ghost.classList.remove("visible");
        if(prefix) prefix.textContent="";
        if(suffix) suffix.textContent="";
    }

    if(menu) {
        menu.innerHTML="";
        if(suggestions.length > 1 || (suggestions.length === 1 && suggestions[0].label !== "Search filter")) {
            suggestions.forEach((item,index) => {
                const row=document.createElement("button");
                row.type="button";
                row.className="search-autocomplete-option" + (index === autocompleteSelection ? " selected" : "");
                row.setAttribute("role","option");
                row.setAttribute("aria-selected", index === autocompleteSelection ? "true" : "false");
                row.innerHTML=`<span class="autocomplete-command"></span><span class="autocomplete-description"></span>`;
                row.querySelector(".autocomplete-command").textContent=item.text;
                row.querySelector(".autocomplete-description").textContent=item.label;
                row.addEventListener("mousedown", event => { event.preventDefault(); acceptSearchAutocomplete(index); });
                menu.appendChild(row);
            });
            menu.classList.add("visible");
            area?.classList.add("autocomplete-open");
        } else {
            menu.classList.remove("visible");
            area?.classList.remove("autocomplete-open");
        }
    }
}

function normalizeSearchValue(value){
    return String(value || "").toLowerCase().trim();
}

function parseModelSearch(raw){
    const tokens=[];
    const tokenRegex=/(exclude|source|src|arch|architecture|type|status|access|downloaded|update|mature|nsfw|media|favorite-creator|favorite|fav|author|creator|tag|sha):(?:"([^"]+)"|(\S+))/gi;
    let match;
    while((match=tokenRegex.exec(raw || "")) !== null){
        const aliases={src:"source",architecture:"arch",creator:"author",nsfw:"mature",fav:"favorite"};
        tokens.push({
            key:aliases[match[1].toLowerCase()] || match[1].toLowerCase(),
            value:normalizeSearchValue(match[2] || match[3]),
            raw:match[0],
            start:match.index,
            end:tokenRegex.lastIndex
        });
    }
    let text=String(raw || "");
    [...tokens].reverse().forEach(token => { text=text.slice(0,token.start)+" "+text.slice(token.end); });
    text=normalizeSearchValue(text.replace(/\s+/g," "));
    return {text,tokens};
}

function getActiveSearchQueries(){
    const queries = [...committedSearchTerms];
    const live = document.getElementById("modelSearch")?.value.trim() || "";
    if(live) queries.push(live);
    return queries;
}


// Plain text is safe to push into SQLite before LIMIT/OFFSET. Structured
// tokens keep their existing client semantics for this small Search-only pass.
window.modelRadarGetBackendSearchText = function(){
    return getActiveSearchQueries()
        .map(parseModelSearch)
        .map(parsed => String(parsed.text || "").trim())
        .filter(Boolean)
        .join(" ")
        .replace(/\s+/g, " ")
        .trim();
};


window.modelRadarGetBackendSourceFilters = function(){
    // A single source:<provider> Search token is structural, just like choosing
    // one source in Options. Resolve it against the real source controls so
    // display labels and canonical values share exactly the same mapping.
    const requested = [];
    getActiveSearchQueries()
        .map(parseModelSearch)
        .forEach(parsed => {
            parsed.tokens
                .filter(token => token.key === "source")
                .forEach(token => requested.push(token.value));
        });

    const unique = Array.from(new Set(
        requested.map(value => normalizedOptionValue(value)).filter(Boolean)
    ));

    // Source checkboxes are OR semantics, while two source: tokens are AND
    // semantics in Search (useful for merged cards). Only translate the common
    // one-source case; leave multi-source-token matching to the browser.
    if(unique.length !== 1) return [];

    const wanted = unique[0];
    const inputs = Array.from(document.querySelectorAll('input[name="sources"]'));
    const target = inputs.find(input =>
        normalizedOptionValue(input.value) === wanted ||
        normalizedOptionValue(input.dataset.sourceLabel) === wanted
    );

    return target ? [target.value] : [];
};


function normalizedOptionValue(value){
    return String(value || "").toLowerCase().replace(/[\s_-]+/g, "").trim();
}

function setSelectFromSearch(id, requested, aliases={}){
    const select=document.getElementById(id);
    if(!select) return false;
    const wanted=normalizedOptionValue(aliases[String(requested || "").toLowerCase()] || requested);
    const option=Array.from(select.options).find(item =>
        normalizedOptionValue(item.value) === wanted || normalizedOptionValue(item.textContent) === wanted
    );
    if(!option) return false;
    select.value=option.value;
    return true;
}

function normalizeSensitiveMode(value){
    return String(value || "hide").toLowerCase() === "show" ? "show" : "hide";
}

function applyStructuredSearchToOptions(rawTerm){
    const term=String(rawTerm || "").trim();
    const match=term.match(/^([a-z-]+):(.*)$/i);
    if(!match) return false;
    const aliases={src:"source",architecture:"arch",nsfw:"mature",fav:"favorite"};
    const key=aliases[match[1].toLowerCase()] || match[1].toLowerCase();
    const value=match[2].trim().replace(/^"|"$/g, "");
    if(!value) return false;

    let changed=false;
    if(key === "source"){
        const wanted=normalizedOptionValue(value);
        const inputs=Array.from(document.querySelectorAll('input[name="sources"]'));
        const target=inputs.find(input =>
            normalizedOptionValue(input.value) === wanted ||
            normalizedOptionValue(input.dataset.sourceLabel) === wanted
        );
        if(!target) return false;
        inputs.forEach(input => input.checked = input === target);
        updateSourcePills();
        updateSourceFilterSummary();
        changed=true;
    } else if(key === "arch"){
        changed=setSelectFromSearch("familyFilter", value);
    } else if(key === "type"){
        changed=setSelectFromSearch("modelTypeFilter", value);
    } else if(key === "status"){
        changed=setSelectFromSearch("statusFilter", value);
    } else if(key === "access"){
        changed=setSelectFromSearch("accessFilter", value, {unknown:"unconfirmed"});
    } else if(key === "mature"){
        const normalized=value.toLowerCase();
        if(["true","yes","1","show"].includes(normalized)){
            document.getElementById("sensitiveFilter").value="show"; changed=true;
        } else if(["false","no","0","hide","blur","only"].includes(normalized)){
            // Legacy Blur/Only values safely collapse to Hide in the two-state UI.
            document.getElementById("sensitiveFilter").value="hide"; changed=true;
        }
    } else if(key === "media"){
        const normalized=value.toLowerCase();
        if(["true","yes","1"].includes(normalized)){
            const media=document.getElementById("showMediaOnly");
            if(media){ media.checked=true; changed=true; }
        }
    } else if(key === "favorite"){
        const normalized=value.toLowerCase();
        const select=document.getElementById("favoriteFilter");
        if(select && ["true","yes","1"].includes(normalized)){ select.value="favorite"; changed=true; }
        else if(select && ["false","no","0"].includes(normalized)){ select.value="not_favorite"; changed=true; }
    } else if(key === "favorite-creator"){
        const normalized=value.toLowerCase();
        const select=document.getElementById("creatorFavoriteFilter");
        if(select && ["true","yes","1"].includes(normalized)){ select.value="favorite"; changed=true; }
        else if(select && ["false","no","0"].includes(normalized)){ select.value="not_favorite"; changed=true; }
    }

    if(changed){
        filterCards();
        savePreferences();
    }
    return changed;
}

function removeCommittedSearchTerm(index){
    committedSearchTerms.splice(index, 1);
    filterCards();
    savePreferences();
    refreshWindowAfterFilterRemoval("committed-search-remove");
}

function tokenMatchesCard(card, token){
    const value=token.value;
    const source=normalizeSearchValue(card.dataset.sources || card.dataset.source).replace(/\s+/g,"");
    const arch=normalizeSearchValue(card.dataset.architecture);
    const type=normalizeSearchValue(card.dataset.type);
    const status=normalizeSearchValue(card.dataset.status);
    const author=normalizeSearchValue(card.dataset.author);
    const tags=normalizeSearchValue(card.dataset.tags);
    const sha=normalizeSearchValue(card.dataset.sha).replace(/[^0-9a-f ]/g, "");
    const gated=card.dataset.gated === "true" || card.dataset.gated === "1";
    const access=(card.dataset.access || (gated ? "gated" : "public")).toLowerCase();
    const sensitive=card.dataset.sensitive === "true" || card.dataset.sensitive === "1";
    const hasSafeSource=card.dataset.hasSafeSource === "true" || card.dataset.hasSafeSource === "1";
    const hasSensitiveSource=card.dataset.hasSensitiveSource === "true" || card.dataset.hasSensitiveSource === "1";
    const media=card.dataset.hasMedia === "true" || card.dataset.hasMedia === "1";
    const favorite=card.dataset.favorite === "true" || card.dataset.favorite === "1";
    const downloaded=card.dataset.downloaded === "true" || card.dataset.downloaded === "1";
    const updateAvailable=card.dataset.update === "true" || card.dataset.update === "1";
    switch(token.key){
        case "source": {
            const wanted=value.replace(/\s+/g,"");
            const sources=source.split(/[,|]/).map(part=>part.trim()).filter(Boolean);
            return sources.includes(wanted);
        }
        case "arch": return arch.includes(value);
        case "type": return type.includes(value);
        case "status": return status === value;
        case "author": return author.includes(value);
        case "tag": return tags.includes(value);
        case "sha": {
            const wanted=value.replace(/[^0-9a-f]/g, "");
            if(!wanted) return true;
            return sha.split(/\s+/).some(hash => hash.includes(wanted));
        }
        case "access":
            if(value === "gated") return access === "gated" || access === "paid_access";
            if(value === "unconfirmed" || value === "unknown") return access === "unconfirmed";
            if(value === "downloadable") return access === "downloadable";
            if(value === "paid_access") return access === "paid_access";
            if(value === "public") return access !== "gated" && access !== "paid_access";
            return true;
        case "mature": return ["1","true","yes","only","mature"].includes(value) ? hasSensitiveSource : ["0","false","no","hide","safe"].includes(value) ? hasSafeSource : true;
        case "media": return ["1","true","yes","only"].includes(value) ? media : ["0","false","no"].includes(value) ? !media : true;
        case "downloaded": return ["1","true","yes","only"].includes(value) ? downloaded : ["0","false","no"].includes(value) ? !downloaded : true;
        case "update": return ["1","true","yes","only"].includes(value) ? updateAvailable : ["0","false","no"].includes(value) ? !updateAvailable : true;
        case "favorite": return ["1","true","yes","only","favorite","starred"].includes(value) ? favorite : ["0","false","no"].includes(value) ? !favorite : true;
        case "favorite-creator": {
            const creatorFavorite = favoriteCreatorNames.has(author);
            return ["1","true","yes","only","favorite"].includes(value) ? creatorFavorite : ["0","false","no"].includes(value) ? !creatorFavorite : true;
        }
        case "exclude": {
            // `exclude:gated` is the simple form. Nested forms such as
            // `exclude:source:tensorhub` and `exclude:arch:flux` are also valid.
            if(value === "gated") return access !== "gated" && access !== "paid_access";
            if(value === "unknown" || value === "unconfirmed") return access !== "unconfirmed";
            if(value === "downloadable") return access !== "downloadable";
            const colon=value.indexOf(":");
            if(colon > 0){
                const nested={key:value.slice(0,colon),value:value.slice(colon+1)};
                return !tokenMatchesCard(card,nested);
            }
            const haystack=[card.dataset.name,card.dataset.author,card.dataset.sources || card.dataset.source,card.dataset.architecture,card.dataset.type,card.dataset.tags,card.dataset.sha]
                .map(normalizeSearchValue).join(" ");
            return !haystack.includes(value);
        }
        default: return true;
    }
}

function searchMatchesCard(card, parsed){
    if(!parsed.tokens.every(token => tokenMatchesCard(card,token))) return false;
    if(!parsed.text) return true;
    const haystack=[card.dataset.name,card.dataset.author,card.dataset.sources || card.dataset.source,card.dataset.architecture,card.dataset.type,card.dataset.tags,card.dataset.sha]
        .map(normalizeSearchValue).join(" ");
    return parsed.text.split(/\s+/).every(term => haystack.includes(term));
}

function refreshWindowAfterFilterRemoval(reason="filter-pill-remove"){
    if(typeof window.modelRadarResetFeedWindow === "function"){
        return window.modelRadarResetFeedWindow({reason});
    }
    return Promise.resolve();
}

function removeSearchToken(rawToken){
    const input=document.getElementById("modelSearch");
    if(!input) return;
    input.value=input.value.replace(rawToken,"").replace(/\s{2,}/g," ").trim();
    filterCards();
    savePreferences();
    refreshWindowAfterFilterRemoval("search-token-remove");
}

function addFilterPill(container,label,onRemove,{removable=true}={}){
    const pill=document.createElement("span"); pill.className="filter-pill";
    const text=document.createElement("span"); text.className="filter-pill-label"; text.textContent=label;
    pill.appendChild(text);

    if(removable){
        const button=document.createElement("button"); button.type="button"; button.textContent="×"; button.setAttribute("aria-label",`Remove ${label} filter`);
        button.addEventListener("click",event=>{event.stopPropagation();onRemove?.();});
        pill.appendChild(button);
    }

    container.appendChild(pill);
}

function renderFilterPills(){
    const container=document.getElementById("activeFilterPills"); if(!container) return;
    container.replaceChildren();
    const structuralFilterIds=new Set([
        "familyFilter",
        "modelTypeFilter",
        "statusFilter",
        "sensitiveFilter",
        "favoriteFilter",
        "creatorFavoriteFilter",
        "downloadStatusFilter"
    ]);

    const resetSelect=(id,value)=>()=>{
        const el=document.getElementById(id);
        if(el) el.value=value;

        filterCards();
        savePreferences();

        if(structuralFilterIds.has(id)){
            refreshWindowAfterFilterRemoval(`pill-remove:${id}`);
        }
    };
    const family=document.getElementById("familyFilter")?.value || "";
    const type=document.getElementById("modelTypeFilter")?.value || "";
    const status=document.getElementById("statusFilter")?.value || "All";
    const access=document.getElementById("accessFilter")?.value || "all";
    const mature=document.getElementById("sensitiveFilter")?.value || "hide";
    const favorite=document.getElementById("favoriteFilter")?.value || "all";
    const creatorFavorite=document.getElementById("creatorFavoriteFilter")?.value || "all";
    const downloadStatus=document.getElementById("downloadStatusFilter")?.value || "all";
    if(family) addFilterPill(container,family,resetSelect("familyFilter",""));
    if(type) addFilterPill(container,type,resetSelect("modelTypeFilter",""));
    if(status.toLowerCase()!=="all") addFilterPill(container,status,resetSelect("statusFilter","All"));
    if(access!=="all"){const labels={downloadable:"Downloadable",unconfirmed:"Unknown",gated:"Gated / No Download",public:"Public"};addFilterPill(container,labels[access]||access,resetSelect("accessFilter","all"));}
    // Maturity is a persistent display preference controlled from Settings.
    // Do not surface it as a removable/active search pill; clearing filters must
    // never change the persistent Hide / Show preference.
    if(favorite!=="all") addFilterPill(container,favorite==="favorite"?"★ Favorites":"Not favorited",resetSelect("favoriteFilter","all"));
    if(creatorFavorite!=="all") addFilterPill(container,creatorFavorite==="favorite"?"Favorite Creators":"Creators not favorited",resetSelect("creatorFavoriteFilter","all"));
    if(downloadStatus!=="all"){
        const downloadLabels={downloaded:"Downloaded",updates:"Updates Available",not_downloaded:"Not Downloaded"};
        addFilterPill(container,downloadLabels[downloadStatus]||downloadStatus,resetSelect("downloadStatusFilter","all"));
    }
    if(document.getElementById("showMediaOnly")?.checked) addFilterPill(container,"Media only",()=>{
        document.getElementById("showMediaOnly").checked=false;
        filterCards();
        savePreferences();
        refreshWindowAfterFilterRemoval("pill-remove:media");
    });

    const sourceInputs=Array.from(document.querySelectorAll('input[name="sources"]'));
    const selected=sourceInputs.filter(input=>input.checked);
    if(sourceInputs.length && selected.length!==sourceInputs.length){
        selected.forEach(input=>addFilterPill(container,`source:${String(input.value).toLowerCase().replace(/\s+/g, "")}`,()=>{
            const currentlySelected=sourceInputs.filter(item=>item.checked);
            if(currentlySelected.length <= 1) sourceInputs.forEach(item=>item.checked=true);
            else input.checked=false;
            updateSourcePills();
            updateSourceFilterSummary();
            filterCards();
            savePreferences();
            refreshWindowAfterFilterRemoval("pill-remove:source");
        }));
    }
    committedSearchTerms.forEach((term,index)=>
        addFilterPill(container,term,()=>removeCommittedSearchTerm(index))
    );
}

function updateSourceFilterSummary(){
    const inputs=Array.from(document.querySelectorAll('input[name="sources"]'));
    const selected=inputs.filter(input=>input.checked);
    const summary=document.getElementById("sourceFilterSummary");
    if(!summary) return;
    if(!selected.length) summary.textContent="No sources";
    else if(selected.length===inputs.length) summary.textContent="All Sources";
    else if(selected.length===1) summary.textContent=selected[0].dataset.sourceLabel || selected[0].value;
    else summary.textContent=`${selected.length} Sources`;
}

function filterCards(){

    const showMediaOnly = document.getElementById("showMediaOnly")?.checked || false;
    const enabledSources = Array.from(document.querySelectorAll('input[name="sources"]:checked'))
        .map(input => input.value.toLowerCase().replace(/\s+/g, ""));

    const family = (document.getElementById("familyFilter")?.value || "").toLowerCase();
    const modelType = (document.getElementById("modelTypeFilter")?.value || "").toLowerCase();
    const status = (document.getElementById("statusFilter")?.value || "all").toLowerCase();
    const access = (document.getElementById("accessFilter")?.value || "all").toLowerCase();
    const sensitiveMode = normalizeSensitiveMode(document.getElementById("sensitiveFilter")?.value);
    const favoriteMode = (document.getElementById("favoriteFilter")?.value || "all").toLowerCase();
    const creatorFavoriteMode = (document.getElementById("creatorFavoriteFilter")?.value || "all").toLowerCase();
    const downloadStatusMode = (document.getElementById("downloadStatusFilter")?.value || "all").toLowerCase();

    const activeSearches = getActiveSearchQueries().map(parseModelSearch);
    const serverWindowed = typeof window.modelRadarResetFeedWindow === "function";

    document.querySelectorAll(".model-card").forEach(card => {
        const sources = (card.dataset.sources || card.dataset.source || "").toLowerCase().split(/\s+/).map(x=>x.replace(/\s+/g,"")).filter(Boolean);
        const arch = (card.dataset.architecture || "").toLowerCase();
        const type = (card.dataset.type || "").toLowerCase();
        const cardStatus = (card.dataset.status || "").toLowerCase();
        const hasMedia = card.dataset.hasMedia === "true" || card.dataset.hasMedia === "1";
        const gated = card.dataset.gated === "true" || card.dataset.gated === "1";
        const cardAccess = (card.dataset.access || (gated ? "gated" : "public")).toLowerCase();
        const sensitive = card.dataset.sensitive === "true" || card.dataset.sensitive === "1";
        const favorite = card.dataset.favorite === "true" || card.dataset.favorite === "1";
        const creatorFavorite = favoriteCreatorNames.has((card.dataset.author || "").toLowerCase());
        const downloaded = card.dataset.downloaded === "true" || card.dataset.downloaded === "1";
        const updateAvailable = card.dataset.update === "true" || card.dataset.update === "1";
        card.classList.remove("sensitive-blurred");

        let visible = true;

        if(showMediaOnly) visible = visible && hasMedia;
        if(enabledSources.length) visible = visible && sources.some(source => enabledSources.includes(source));
        // Once feed windowing is active, architecture/model type are already
        // applied in SQLite before LIMIT/OFFSET. Do not narrow them a second time
        // from canonical card metadata: merged cards can qualify through another
        // source snapshot even when the canonical row has a different label.
        if(!serverWindowed && family) visible = visible && arch.includes(family);
        if(!serverWindowed && modelType) visible = visible && type.includes(modelType);
        if(status === "updated") {
            visible = visible && (card.dataset.latestUpdated === "true" || card.dataset.latestUpdated === "1");
        } else if(status !== "all" && status !== "") {
            visible = visible && cardStatus === status;
        }
        if(access === "downloadable") visible = visible && cardAccess === "downloadable";
        if(access === "paid_access") visible = visible && cardAccess === "paid_access";
        if(access === "unconfirmed") visible = visible && cardAccess === "unconfirmed";
        if(access === "public") visible = visible && !["gated","paid_access"].includes(cardAccess);
        if(access === "gated") visible = visible && ["gated","paid_access"].includes(cardAccess);
        if(sensitiveMode === "hide") visible = visible && !sensitive;
        if(favoriteMode === "favorite") visible = visible && favorite;
        if(favoriteMode === "not_favorite") visible = visible && !favorite;
        if(creatorFavoriteMode === "favorite") visible = visible && creatorFavorite;
        if(creatorFavoriteMode === "not_favorite") visible = visible && !creatorFavorite;
        if(downloadStatusMode === "downloaded") visible = visible && downloaded;
        if(downloadStatusMode === "updates") visible = visible && updateAvailable;
        if(downloadStatusMode === "not_downloaded") visible = visible && !downloaded;
        visible = visible && activeSearches.every(parsed => searchMatchesCard(card, parsed));

        card.classList.toggle("hidden", !visible);
    });

    updateNavbarCounts();
    updateFilterUI();
}

function navbarCountRequestState(){
    const sourceInputs = Array.from(document.querySelectorAll('input[name="sources"]'));
    const selectedSources = sourceInputs.filter(input => input.checked).map(input => input.value);
    const searchSources = window.modelRadarGetBackendSourceFilters?.() || [];

    return {
        architecture: document.getElementById("familyFilter")?.value || "",
        modelType: document.getElementById("modelTypeFilter")?.value || "",
        status: (document.getElementById("statusFilter")?.value || "All").toLowerCase(),
        media: document.getElementById("showMediaOnly")?.checked || false,
        favorite: (document.getElementById("favoriteFilter")?.value || "all").toLowerCase(),
        creatorFavorite: (document.getElementById("creatorFavoriteFilter")?.value || "all").toLowerCase(),
        downloadStatus: (document.getElementById("downloadStatusFilter")?.value || "all").toLowerCase(),
        searchText: window.modelRadarGetBackendSearchText?.() || "",
        // Sending no source list means "all sources". Only send a list when the
        // user actually narrowed the provider selection.
        sources: searchSources.length
            ? searchSources
            : (sourceInputs.length && selectedSources.length !== sourceInputs.length
                ? selectedSources
                : [])
    };
}

function applyNavbarCountData(data){
    if(!data) return;
    const modelCount = document.getElementById("modelCountValue");
    const newCount = document.getElementById("newCountValue");
    const updatedCount = document.getElementById("updatedCountValue");

    if(modelCount && Number.isFinite(Number(data.total))){
        modelCount.textContent = String(Number(data.total));
    }
    if(newCount && Number.isFinite(Number(data.new))){
        newCount.textContent = String(Number(data.new));
    }
    if(updatedCount && Number.isFinite(Number(data.updated))){
        updatedCount.textContent = String(Number(data.updated));
    }
}

function requestTrueNavbarCounts(){
    // Creator pages have their own creator-local count semantics.
    if(document.body.classList.contains("creator-page")) return;

    const state = navbarCountRequestState();
    const signature = JSON.stringify(state);

    if(signature === lastNavbarCountSignature && lastNavbarCountData){
        applyNavbarCountData(lastNavbarCountData);
        return;
    }

    clearTimeout(navbarCountTimer);
    navbarCountTimer = setTimeout(async () => {
        const requestId = ++navbarCountRequestId;
        try{
            const url = new URL("/feed/counts", window.location.origin);
            if(state.architecture) url.searchParams.set("architecture", state.architecture);
            if(state.modelType) url.searchParams.set("model_type", state.modelType);
            if(state.status && state.status !== "all") url.searchParams.set("status", state.status);
            if(state.media) url.searchParams.set("media", "1");
            if(state.favorite !== "all") url.searchParams.set("favorite", state.favorite);
            if(state.creatorFavorite !== "all") url.searchParams.set("creator_favorite", state.creatorFavorite);
            if(["downloaded","updates","not_downloaded"].includes(state.downloadStatus)) url.searchParams.set("download_status", state.downloadStatus);
            if(state.searchText) url.searchParams.set("search", state.searchText);
            if(state.sources.length) url.searchParams.set("sources", state.sources.join(","));

            const response = await fetch(url, {cache:"no-store"});
            const data = await response.json();
            if(requestId !== navbarCountRequestId) return;
            if(!response.ok || !data.success) return;

            lastNavbarCountSignature = signature;
            lastNavbarCountData = data;
            applyNavbarCountData(data);
        }catch(error){
            console.debug("Unable to refresh true navbar counts:", error);
        }
    }, 70);
}

function updateNavbarCounts(){
    const feed = document.getElementById("modelFeed");

    // No filter at all: use the full totals already supplied by Flask with the
    // initial page. This is instant and never changes as chunks are appended.
    if(feed && !filtersAreActive()){
        applyNavbarCountData({
            total: Number(feed.dataset.totalModelCount || 0),
            new: Number(feed.dataset.totalNewCount || 0),
            updated: Number(feed.dataset.totalUpdatedCount || 0)
        });
        return;
    }

    // Active structural filters must never fall back to visible.length. The
    // browser may currently hold only 120 of thousands of matching cards.
    requestTrueNavbarCounts();
}

window.modelRadarFilterCards = filterCards;

function savePreferences(){
    // Creator pages intentionally use temporary, page-local filtering. Home
    // feed filters should neither leak into a creator page nor be overwritten
    // by temporary filtering performed while browsing one creator.
    if(document.body.classList.contains("creator-page")) return;

    // Searching can fire an input event for every keystroke. Debounce writes so
    // a threaded Flask server never receives a burst of simultaneous settings saves.
    clearTimeout(preferenceSaveTimer);
    preferenceSaveTimer = setTimeout(persistPreferences, 250);
}

function persistPreferences(){
    const sources = Array.from(document.querySelectorAll('input[name="sources"]:checked'))
        .map(input => input.value);

    const preferences = {
        selected_sources: sources,
        selected_architecture: document.getElementById("familyFilter")?.value || "",
        selected_model_type: document.getElementById("modelTypeFilter")?.value || "",
        selected_status: document.getElementById("statusFilter")?.value || "All",
        selected_access: document.getElementById("accessFilter")?.value || "all",
        selected_sensitive: normalizeSensitiveMode(document.getElementById("sensitiveFilter")?.value),
        selected_favorite: document.getElementById("favoriteFilter")?.value || "all",
        selected_creator_favorite: document.getElementById("creatorFavoriteFilter")?.value || "all",
        selected_download_status: document.getElementById("downloadStatusFilter")?.value || "all",
        // Scanner sources intentionally mirror the feed source selection.
        selected_scan_sources: sources,
        show_media_only: document.getElementById("showMediaOnly")?.checked || false,
        selected_search: document.getElementById("modelSearch")?.value || "",
        selected_search_terms: committedSearchTerms
    };

    window.userPreferences = Object.assign({}, window.userPreferences || {}, preferences);

    fetch("/save_preferences", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(preferences)
    }).catch(error => console.error("Unable to save AbyssBeacon preferences:", error));
}

function restorePreferences(){
    if(document.body.classList.contains("creator-page")){
        // Creator pages always begin with the creator's complete stored set.
        // This prevents a Home filter such as New from making a just-opened
        // (therefore now Seen) model disappear from its creator page.
        document.querySelectorAll('input[name="sources"]').forEach(input => {
            input.checked = true;
        });
        const defaults = {
            familyFilter: "",
            modelTypeFilter: "",
            statusFilter: "All",
            accessFilter: "all",
            sensitiveFilter: "hide",
            favoriteFilter: "all",
            creatorFavoriteFilter: "all",
            downloadStatusFilter: "all"
        };
        Object.entries(defaults).forEach(([id, value]) => {
            const element = document.getElementById(id);
            if(element) element.value = value;
        });
        const media = document.getElementById("showMediaOnly");
        if(media) media.checked = false;
        const search = document.getElementById("modelSearch");
        if(search) search.value = "";
        committedSearchTerms = [];
        return;
    }

    const preferences = window.userPreferences || {};

    if(Array.isArray(preferences.selected_sources)){
        document.querySelectorAll('input[name="sources"]').forEach(input => {
            input.checked = preferences.selected_sources.includes(input.value);
        });
    }


    const values = {
        familyFilter: preferences.selected_architecture || "",
        modelTypeFilter: preferences.selected_model_type || "",
        statusFilter: preferences.selected_status || "All",
        accessFilter: preferences.selected_access || "all",
        sensitiveFilter: normalizeSensitiveMode(preferences.selected_sensitive),
        favoriteFilter: preferences.selected_favorite || "all",
        creatorFavoriteFilter: preferences.selected_creator_favorite || "all",
        downloadStatusFilter: preferences.selected_download_status || "all"
    };

    Object.entries(values).forEach(([id, value]) => {
        const element = document.getElementById(id);
        if(element) element.value = value;
    });

    const media = document.getElementById("showMediaOnly");
    if(media) media.checked = Boolean(preferences.show_media_only);
    const restoredTerms = Array.isArray(preferences.selected_search_terms)
        ? preferences.selected_search_terms.filter(Boolean)
        : [];
    committedSearchTerms = [];
    restoredTerms.forEach(term => {
        if(!applyStructuredSearchToOptions(term)) committedSearchTerms.push(term);
    });
    const search = document.getElementById("modelSearch");
    if(search) search.value = preferences.selected_search || "";
}

function getVisibleCards(){
    return Array.from(document.querySelectorAll(".model-card:not(.hidden)"));
}

function filtersAreActive(){
    const prefs = {
        family: document.getElementById("familyFilter")?.value || "",
        type: document.getElementById("modelTypeFilter")?.value || "",
        status: (document.getElementById("statusFilter")?.value || "All").toLowerCase(),
        access: (document.getElementById("accessFilter")?.value || "all").toLowerCase(),
        sensitive: normalizeSensitiveMode(document.getElementById("sensitiveFilter")?.value),
        favorite: (document.getElementById("favoriteFilter")?.value || "all").toLowerCase(),
        creatorFavorite: (document.getElementById("creatorFavoriteFilter")?.value || "all").toLowerCase(),
        downloadStatus: (document.getElementById("downloadStatusFilter")?.value || "all").toLowerCase(),
        media: document.getElementById("showMediaOnly")?.checked || false
    };
    const allSources = document.querySelectorAll('input[name="sources"]').length;
    const selectedSources = document.querySelectorAll('input[name="sources"]:checked').length;
    const search=(document.getElementById("modelSearch")?.value || "").trim();
    return Boolean(search || committedSearchTerms.length || prefs.family || prefs.type || prefs.media || prefs.status !== "all" || prefs.access !== "all" || prefs.sensitive !== "show" || prefs.favorite !== "all" || prefs.creatorFavorite !== "all" || prefs.downloadStatus !== "all" || (allSources && selectedSources !== allSources));
}

function updateFilterUI(){
    const active=filtersAreActive();
    document.getElementById("activeFilterDot")?.classList.toggle("hidden", !active);
    document.getElementById("clearAllSearchFilters")?.classList.toggle("hidden", !active);
    renderFilterPills();
    const count = document.querySelectorAll("#activeFilterPills .filter-pill").length;
    const countEl = document.getElementById("optionsFilterCount");
    if(countEl){ countEl.textContent=String(count); countEl.classList.toggle("hidden", count===0); }
    const empty = document.getElementById("emptyFilterState");
    if(empty) empty.classList.toggle("hidden", getVisibleCards().length !== 0 || document.querySelectorAll(".model-card").length === 0);

    // Navbar labels use one interaction language: white at rest, cyan on
    // hover/focus. The filter pill itself already communicates active state.
    ["modelCount", "newCount", "updatedCount"].forEach(id => {
        document.getElementById(id)?.classList.remove("is-active");
    });
}

function resetFilters(){
    document.querySelectorAll('input[name="sources"]').forEach(input => input.checked = true);
    // Maturity is a persistent safety/display preference, not a disposable search filter.
    // Clear All intentionally leaves the Hide/Show maturity preference unchanged.
    const defaults = {familyFilter:"", modelTypeFilter:"", statusFilter:"All", accessFilter:"all", favoriteFilter:"all", creatorFavoriteFilter:"all", downloadStatusFilter:"all"};
    Object.entries(defaults).forEach(([id,value]) => { const el=document.getElementById(id); if(el) el.value=value; });
    const media=document.getElementById("showMediaOnly"); if(media) media.checked=false;
    const search=document.getElementById("modelSearch"); if(search) search.value="";
    committedSearchTerms = [];
    updateSourcePills();
    updateSourceFilterSummary();
    filterCards();
    savePreferences();
    refreshWindowAfterFilterRemoval("reset-all-filters");
}

function updateWindowedNewTotal(changed, {all=false} = {}){
    const feed = document.getElementById("modelFeed");
    if(!feed) return;

    const current = Number(feed.dataset.totalNewCount || 0);
    feed.dataset.totalNewCount = String(
        all ? 0 : Math.max(0, current - Math.max(0, Number(changed || 0)))
    );
}

function normalizeSeenCards(cards){
    return Array.from(cards || []).filter(card =>
        card instanceof Element
        && card.classList.contains("model-card")
    );
}

/**
 * Single frontend entry point for turning model cards from New -> Seen.
 *
 * The database operation may happen elsewhere (for example /model/<id>
 * already marks a card viewed when it is opened). This helper owns the live
 * browser state: badges, data-status, the true windowed New total, navbar
 * counts, and New-filter window replacement.
 *
 * Keeping this centralized also gives the future "mark visible models as seen"
 * observer one reusable batch API instead of creating separate Seen logic.
 */
async function applySeenState(
    cards,
    {
        changed=null,
        all=false,
        refreshNewWindow=true
    } = {}
){
    const normalized = normalizeSeenCards(cards);
    const newlySeen = normalized.filter(
        card => String(card.dataset.status || "").toLowerCase() === "new"
    );

    normalized.forEach(card => {
        card.dataset.status = "seen";
        card.querySelector(".badge-new")?.remove();
    });

    const effectiveChanged = changed == null
        ? newlySeen.length
        : Math.max(0, Number(changed || 0));

    updateWindowedNewTotal(effectiveChanged, {all});
    filterCards();

    // If the user is currently browsing New, a Seen card no longer belongs in
    // this SQL window. Replace the batch so the next New card can slide in.
    const status = String(
        document.getElementById("statusFilter")?.value || "All"
    ).toLowerCase();

    if(refreshNewWindow && status === "new"){
        // Opening a New card used to replace the entire feed with offset 0.
        // Deep in a lazy-loaded result set that collapsed the document and
        // threw the user back toward the previous page break. Remove only the
        // cards that just left the New result set, keep the current viewport,
        // and let Feed Windowing refill from the current logical offset.
        newlySeen.forEach(card => card.remove());
        window.modelRadarReconcileFeedWindow?.(effectiveChanged);
    }

    return {
        cards: normalized,
        newlySeen,
        changed: effectiveChanged
    };
}

async function markModelIdsSeen(ids){
    const uniqueIds = Array.from(new Set(
        Array.from(ids || [])
            .map(id => String(id || "").trim())
            .filter(Boolean)
    ));

    if(!uniqueIds.length){
        return {success:true, changed:0};
    }

    const response = await fetch("/models/mark-seen", {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({ids:uniqueIds})
    });

    const data = await response.json();
    if(!response.ok || !data.success){
        throw new Error(data.error || "Unable to mark models as seen.");
    }

    return data;
}

// Public hooks intentionally kept small for other UI modules and the planned
// viewport-based Seen option.
window.modelRadarApplySeenState = applySeenState;
window.modelRadarMarkModelIdsSeen = markModelIdsSeen;

function requestMarkSeen(mode){
    const allMode = mode === "all";
    const cards = allMode
        ? Array.from(document.querySelectorAll(".model-card"))
        : getVisibleCards();

    const newCards = cards.filter(
        card => (card.dataset.status || "").toLowerCase() === "new"
    );

    const feed = document.getElementById("modelFeed");
    const trueNewCount = Number(
        feed?.dataset.totalNewCount
        || document.getElementById("newCountValue")?.textContent
        || 0
    );

    if(!allMode && !newCards.length) return;
    if(allMode && trueNewCount <= 0) return;

    const execute = async () => {
        const response = await fetch("/models/mark-seen", {
            method:"POST",
            headers:{"Content-Type":"application/json"},
            body:JSON.stringify(
                allMode
                    ? {all:true}
                    : {ids:newCards.map(card => card.dataset.id)}
            )
        });

        const data = await response.json();
        if(!response.ok || !data.success){
            throw new Error(data.error || "Unable to mark models as seen.");
        }

        const changed = Number(data.changed || 0);
        const cardsToUpdate = allMode
            ? Array.from(document.querySelectorAll('.model-card[data-status="new"]'))
            : newCards;

        await applySeenState(
            cardsToUpdate,
            {
                changed,
                all:allMode,
                refreshNewWindow:true
            }
        );
    };

    if(!allMode){
        execute().catch(error => console.error("Mark Visible as Seen failed:", error));
        return;
    }

    if(localStorage.getItem("modelradar_skip_seen_confirmation") === "1"){
        execute().catch(error => console.error("Mark All as Seen failed:", error));
        return;
    }

    const overlay=document.getElementById("seenConfirmOverlay");
    const text=document.getElementById("seenConfirmText");
    const accept=document.getElementById("seenConfirmAccept");
    const cancel=document.getElementById("seenConfirmCancel");
    const check=document.getElementById("seenConfirmDontAsk");

    if(text){
        text.textContent = `This will mark all ${trueNewCount} new models in your library as seen.`;
    }

    overlay?.classList.add("open");
    overlay?.setAttribute("aria-hidden","false");
    if(check) check.checked=false;

    const close=()=>{
        overlay?.classList.remove("open");
        overlay?.setAttribute("aria-hidden","true");
    };

    if(cancel) cancel.onclick=close;
    if(accept){
        accept.onclick=()=>{
            if(check?.checked){
                localStorage.setItem("modelradar_skip_seen_confirmation","1");
            }
            close();
            execute().catch(error => console.error("Mark All as Seen failed:", error));
        };
    }
}
