/* Repository Markdown is rendered with text nodes; remote HTML is never executed. */
(() => {
    function safeLink(value, payload) {
        try {
            const base = payload.source_url;
            if (payload.source === 'modelscope' && !/^[a-z][a-z0-9+.-]*:|^\/\//i.test(value) && !value.startsWith('#')) {
                const relative = new URL(value, 'https://repository.invalid/' + payload.path);
                const url = new URL(base);
                url.searchParams.set('FilePath', decodeURIComponent(relative.pathname.slice(1)));
                return url.href;
            }
            const url = new URL(value, base);
            return ['https:', 'http:'].includes(url.protocol) ? url.href : null;
        } catch (_) { return null; }
    }
    function inline(parent, text, payload, depth = 0) {
        if (depth > 3) { parent.append(document.createTextNode(text)); return; }
        const token = /(!?\[([^\]\n]*)\]\(([^\s)]+)(?:\s+"[^"]*")?\)|`([^`]+)`|\*\*([^*]+)\*\*)/g;
        let match, offset = 0;
        while ((match = token.exec(text))) {
            parent.append(document.createTextNode(text.slice(offset, match.index)));
            if (match[2] !== undefined) {
                const href = safeLink(match[3], payload);
                const node = document.createElement(href ? 'a' : 'span');
                if (href) { node.href = href; node.target = '_blank'; node.rel = 'noopener noreferrer'; }
                node.textContent = (match[1].startsWith('!') ? 'Image: ' : '') + (match[2] || 'View image');
                parent.append(node);
            } else {
                const node = document.createElement(match[4] !== undefined ? 'code' : 'strong');
                if (match[4] !== undefined) node.textContent = match[4];
                else inline(node, match[5], payload, depth + 1);
                parent.append(node);
            }
            offset = token.lastIndex;
        }
        parent.append(document.createTextNode(text.slice(offset)));
    }
    window.modelRadarRenderReadme = (container, payload) => {
        container.replaceChildren();
        let text = String(payload.text || '').replace(/^\uFEFF/, '');
        const metadata = text.match(/^---\s*\r?\n([\s\S]*?)\r?\n---\s*(?:\r?\n|$)/);
        if (metadata) {
            const details = document.createElement('details'), summary = document.createElement('summary'), pre = document.createElement('pre');
            summary.textContent = 'Model card metadata'; pre.textContent = metadata[1];
            details.append(summary, pre); container.append(details); text = text.slice(metadata[0].length);
        }
        const lines = text.split(/\r?\n/);
        let index = 0, list = null;
        while (index < lines.length) {
            const line = lines[index++];
            if (!line.trim()) { list = null; continue; }
            const fence = line.match(/^\s*(```+|~~~+)/);
            if (fence) {
                const code = [];
                while (index < lines.length && !lines[index].trim().startsWith(fence[1])) code.push(lines[index++]);
                index++;
                const pre = document.createElement('pre'), child = document.createElement('code');
                child.textContent = code.join('\n'); pre.append(child); container.append(pre); list = null; continue;
            }
            if (line.includes('|') && index < lines.length && /^\s*\|?\s*:?-{3,}:?\s*\|/.test(lines[index])) {
                const table = document.createElement('table');
                const row = (value, heading) => {
                    const tr = document.createElement('tr');
                    value.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').slice(0, 40).forEach(cell => {
                        const td = document.createElement(heading ? 'th' : 'td'); inline(td, cell.trim(), payload); tr.append(td);
                    });
                    table.append(tr);
                };
                row(line, true); index++;
                while (index < lines.length && lines[index].trim() && lines[index].includes('|')) row(lines[index++], false);
                container.append(table); list = null; continue;
            }
            const heading = line.match(/^(#{1,6})\s+(.+)/);
            const bullet = line.match(/^\s*([-*+] |\d+\. )(.+)/);
            let node;
            if (heading) { node = document.createElement('h' + heading[1].length); inline(node, heading[2], payload); list = null; }
            else if (bullet) {
                const tag = /^\d/.test(bullet[1]) ? 'ol' : 'ul';
                if (!list || list.tagName.toLowerCase() !== tag) { list = document.createElement(tag); container.append(list); }
                node = document.createElement('li'); inline(node, bullet[2], payload); list.append(node); continue;
            } else {
                node = document.createElement(line.startsWith('> ') ? 'blockquote' : 'p');
                inline(node, line.replace(/^> /, ''), payload); list = null;
            }
            container.append(node);
        }
        if (!container.childNodes.length) container.textContent = 'This README is empty.';
    };
    document.addEventListener('DOMContentLoaded', () => {
        const select = document.getElementById('collectionReadmeSelect');
        if (!select) return;
        const status = document.getElementById('collectionModelCardStatus');
        const content = document.getElementById('collectionModelCardContent');
        const link = document.getElementById('collectionModelCardSource');
        let controller, generation = 0;
        async function load() {
            controller?.abort(); controller = new AbortController(); const current = ++generation;
            link.href = select.selectedOptions[0].dataset.sourceUrl;
            document.getElementById('collectionReadmeRetry').hidden = true;
            status.textContent = 'Loading README…'; content.replaceChildren();
            try {
                const response = await fetch(select.value, {signal: controller.signal});
                const payload = await response.json();
                if (current !== generation) return;
                if (!response.ok || !payload.success) throw new Error(payload.error || 'Unable to load README.');
                window.modelRadarRenderReadme(content, payload);
                status.textContent = payload.truncated ? 'Showing the first 512 KiB. View the complete README on the source.' : 'Repository text. Image references open on the source.';
            } catch (error) {
                if (current === generation && error.name !== 'AbortError') {
                    status.textContent = error.message || 'Unable to load README.';
                    document.getElementById('collectionReadmeRetry').hidden = false;
                }
            }
        }
        select.addEventListener('change', load);
        document.getElementById('collectionReadmeRetry').addEventListener('click', load);
        load();
    });
})();
