"""Explicit supported page URLs -> native source lookups; never fetch arbitrary URLs."""
from __future__ import annotations
import re
from urllib.parse import urlsplit, unquote, parse_qs
import scan_control

HOSTS = {'huggingface.co':'huggingface','modelscope.cn':'modelscope','modelscope.ai':'modelscope',
         'civitai.com':'civitai','civitai.red':'civitaired','tensorhub.art':'tensorhub',
         'seaart.ai':'seaart'}


def parse_target(value):
    try:
        parts=urlsplit(str(value or '').strip())
        host=(parts.hostname or '').lower().removeprefix('www.')
        if parts.scheme not in {'http','https'} or host not in HOSTS or parts.username or parts.password or parts.port not in (None,80,443):
            raise ValueError('Paste a model or creator page from a supported source.')
        path=[unquote(x) for x in parts.path.strip('/').split('/') if x]
        if any(not re.fullmatch(r'[A-Za-z0-9_.@-]+',x) or x in {'.','..'} for x in path):
            raise ValueError('Unsupported page URL.')
        source=HOSTS[host];kind='';key=''
        if source=='huggingface':
            if len(path)==1 and path[0] not in {'models','datasets','spaces','collections','settings','organizations'}:
                kind,key='creator',path[0]
            elif len(path)>=2 and path[0] not in {'models','datasets','spaces','collections','api','settings','organizations'} and (len(path)==2 or path[2] in {'tree','blob','resolve','discussions'}):
                kind,key='model','/'.join(path[:2])
        elif source=='modelscope':
            if len(path)>=3 and path[0]=='models':kind,key='model','/'.join(path[1:3])
            elif len(path)==2 and path[0] in {'profile','organization'}:kind,key='creator',path[1]
        elif source in {'civitai','civitaired'}:
            if len(path)>=2 and path[0]=='models' and path[1].isdigit():kind,key='model',path[1]
            elif len(path)>=2 and path[0]=='user':kind,key='creator',path[1]
        elif source=='tensorhub':
            if len(path)>=2 and path[0]=='models' and path[1].isdigit():kind,key='model',path[1]
            elif len(path)==2 and path[0]=='u' and path[1].isdigit():kind,key='creator',path[1]
        elif source=='seaart':
            if path and re.fullmatch(r'[a-z]{2}',path[0]):path=path[1:]
            if len(path)>=3 and path[:2] in [['models','detail'],['model','detail']]:kind,key='model',path[2]
            elif len(path)==2 and path[0]=='user' and re.fullmatch(r'[0-9a-fA-F]{32}',path[1]):kind,key='creator',path[1]
        if not key:raise ValueError('This link is not a recognized model or creator page. Copy its main page URL.')
        version=''
        if source in {'civitai','civitaired'}:
            version=parse_qs(parts.query).get('modelVersionId',[''])[0]
            if version and not version.isdigit():raise ValueError('Invalid model version in URL.')
        canonical_host={'modelscope':'modelscope.cn','seaart':'www.seaart.ai'}.get(source,host)
        if source=='huggingface':route=key
        elif source=='modelscope':route=('models/' if kind=='model' else 'profile/')+key
        elif source in {'civitai','civitaired'}:route=('models/' if kind=='model' else 'user/')+key
        elif source=='tensorhub':route=('models/' if kind=='model' else 'u/')+key
        else:route=('models/detail/' if kind=='model' else 'user/')+key
        canonical='https://'+canonical_host+'/'+route
        if version:canonical+='?modelVersionId='+version
        return dict(source=source,kind=kind,key=key,version=version,url=canonical)
    except (ValueError,TypeError) as exc:
        raise ValueError(str(exc) or 'Invalid page URL.') from exc


def _require(model):
    if not model or not str(getattr(model,'model_key','') or ''):
        raise RuntimeError('The source did not return this model. Check its page and account connection.')
    return model


def scan_target(target, settings):
    result = _scan_target(target, settings)
    scan_control.check_cancelled()
    blocked = {str(x).casefold() for x in settings.get('_blocked_creators', [])}
    result = [m for m in result if str(getattr(m, 'author', '') or '').casefold() not in blocked]
    return result[:max(1, int(settings.get('max_results', 200)))]


def _scan_target(target, settings):
    # Revalidate even internal jobs. Caller-supplied URLs never become API endpoints.
    target=parse_target(target['url']);source=target['source'];key=target['key']
    from scanners import ALL_SCANNERS
    module=ALL_SCANNERS[source]
    scan_control.check_cancelled()
    if target['kind']=='creator' and source not in {'tensorhub','seaart'}:
        return module.scan('',set(),{**settings, '_url_creator_limit': True},creator=key)
    if source in {'civitai','civitaired'}:
        if source=='civitai':
            module._apply_auth();module._DETAIL_ENRICHMENT_DISABLED=False
            detail=module._fetch_model_detail(key)
            if not detail:raise RuntimeError('CivitAI model details unavailable or rate limited.')
            if target['version']:detail['_selected_version_id']=target['version']
            model=module._build_model(detail,enrich=True,
                                      include_mature_media=bool(settings.get('include_mature_media',False)),
                                      media_limit=int(settings.get('_media_limit',100)))
        else:
            seed={'id':key,'_force_red_page':True}
            if target['version']:seed['version']={'id':target['version']}
            model=module._build_model(seed,enrich=True)
        if target['version'] and str((model.card_data or {}).get('version_id') or '') != target['version']:
            raise RuntimeError('The source did not return the requested model version.')
        return [_require(model)]
    if source=='seaart':
        from contextlib import nullcontext
        context=module.live_session(cancellable=True) if module.browser_session_saved() else nullcontext(None)
        with context as live:
            ids=[key]
            if target['kind']=='creator':
                cards=module._fetch_creator_catalog(key,settings,live=live)
                ids=[str(c.get('id') or c.get('model_id') or c.get('model_no') or '') for c in cards]
            result=[]
            for model_id in ids:
                scan_control.check_cancelled()
                if model_id:result.append(_require(module._build(module._detail(model_id,live=live))))
            return result
    if source=='tensorhub':
        module._apply_auth()
        if target['kind']=='creator':
            items=module._fetch_creator_catalog(key,int(settings.get('max_results',200)),set(settings.get('_blocked_creators',[])))
            seeds=[module._build_model(item,discovery_lane='url-creator') for item in items]
        else:
            r=module._get_with_backoff(module.DETAIL_API,{'modelId':key},'URL model detail')
            r.raise_for_status();payload=r.json()
            data=payload.get('data') or {}
            if str(payload.get('code','0'))!='0' or not data.get('project') or not data.get('model'):
                raise RuntimeError('TensorHub model details unavailable; check the exact version page URL.')
            item=dict(data['project']);item['model']=data['model']
            seeds=[module._build_model(item,discovery_lane='url-model')]
        result=[]
        for seed in seeds:
            scan_control.check_cancelled();seed=_require(seed)
            model,ok,reason=module._fetch_public_detail(seed,discovery_lane='url-import')
            if not ok:raise RuntimeError('TensorHub detail lookup failed: '+str(reason))
            result.append(_require(model))
        return result
    if source=='modelscope':
        details=module.get_details(key)
        if not details:raise RuntimeError('ModelScope repository not found or unavailable.')
        return [_require(module._build_tag_discovery_model({'model_id':key,**details}))]
    if source=='huggingface':
        from scanners.common import processors, media, metadata
        module._apply_auth()
        r=module.get_with_backoff(module.session,'https://huggingface.co/api/models/'+key,
                                 provider='Hugging Face',label='URL repository detail',params={'blobs':'true'},timeout=20)
        r.raise_for_status();details=r.json()
        if str(details.get('id') or '').casefold()!=key.casefold():raise RuntimeError('Hugging Face returned a different repository.')
        files,complete,method=module.repository_files_with_status(key,details,force_recursive=True)
        revision=str(details.get('sha') or 'main')
        readme=''
        rr=module.get_with_backoff(module.session,f'https://huggingface.co/{key}/raw/{revision}/README.md',
                                  provider='Hugging Face',label='URL repository README',timeout=15)
        if rr.status_code==200:readme=rr.text[:module.MAX_README_BYTES]
        details['readme']=readme
        images=media.extract_media(files,f'https://huggingface.co/{key}/resolve/{revision}')
        embeds=module.fetch_repository_readme_media(key,files,root_readme_text=readme,revision=revision,include_nested=False)
        items=module.merge_media_items(images.get('media') or [],embeds)
        summary=module.media_summary(items)
        card=module.repository_inventory_marker(details.get('cardData') or {},complete=complete,revision=revision,method=method,file_count=len(files))
        card=module.readme_media_marker(card,embeds)
        raw=dict(model_id=key,model_key=key.lower(),details=details,files=files,tags=details.get('tags') or [],source=source,
                 url=target['url'],card_data=card,media=items,image=summary.get('image',''),
                 preview_count=len(items),has_media=bool(items),has_video=summary.get('has_video',False),
                 gated=bool(details.get('gated')),sha=revision,created=details.get('createdAt',''),updated=details.get('lastModified',''))
        raw['sensitive'] = metadata.detect_sensitive(key, raw['tags'], card, details.get('tags') or [])
        return [_require(processors.build_model(raw))]
    raise ValueError('Unsupported source')
