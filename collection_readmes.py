"""Bounded, native-source README lookups for stored collection identities."""
import json
import logging
import re
from urllib.parse import quote,urlencode

MAX_README_BYTES=512*1024
SOURCE_LABELS={'huggingface':'Hugging Face','modelscope':'ModelScope'}


def repository_files(snapshot):
    files=snapshot.get('files') or []
    if isinstance(files,str):
        try:files=json.loads(files)
        except (TypeError,ValueError):files=[]
    return [item for item in files if isinstance(item,dict)] if isinstance(files,list) else []


def source_snapshots(canonical,snapshots):
    result=[];seen=set()
    for snapshot in list(snapshots)+[canonical]:
        source=str(snapshot.get('source') or '').lower().strip()
        key=str(snapshot.get('model_key') or '').strip()
        parts=key.split('/')
        if (source not in SOURCE_LABELS or source in seen or len(parts)!=2
            or any(part in {'.','..'} or not re.fullmatch(r'[\w.-]+',part) for part in parts)):
            continue
        seen.add(source);result.append(dict(snapshot,source=source,model_key=key))
    return result


def readme_target(snapshot,path='README.md'):
    source=snapshot['source'];key=snapshot['model_key']
    path=str(path or '').strip().replace('\\','/')
    if (not path or path.startswith('/') or any(part in {'','..','.'} for part in path.split('/'))
        or path.rsplit('/',1)[-1].casefold()!='readme.md'):
        raise ValueError('Invalid README path.')
    selected=next((item for item in repository_files(snapshot)
        if str(item.get('path') or item.get('name') or '').replace('\\','/').casefold()==path.casefold()),None)
    if selected:
        path=str(selected.get('path') or selected.get('name')).replace('\\','/')
    elif '/' in path:
        raise LookupError('README not found in this Collection snapshot.')
    # Root READMEs are often omitted from an older model/file snapshot. Native
    # root lookup is allowed without forcing a model reload or inventory scan.
    revision=str((selected or {}).get('revision') or snapshot.get('revision')
                 or (snapshot.get('sha') if source=='huggingface' else '')
                 or ('main' if source=='huggingface' else 'master'))
    encoded_key=quote(key,safe='/');encoded_path=quote(path,safe='/');encoded_revision=quote(revision,safe='')
    if source=='huggingface':
        raw=f'https://huggingface.co/{encoded_key}/raw/{encoded_revision}/{encoded_path}'
        source_url=f'https://huggingface.co/{encoded_key}/blob/{encoded_revision}/{encoded_path}'
        media_url=f'https://huggingface.co/{encoded_key}/resolve/{encoded_revision}/{encoded_path}'
        params=None
    else:
        raw=f'https://modelscope.cn/api/v1/models/{encoded_key}/repo'
        params={'Revision':revision,'FilePath':path}
        source_url=f'https://modelscope.cn/models/{encoded_key}/files?'+urlencode(params)
        media_url=raw+'?'+urlencode(params)
    return dict(source=source,source_label=SOURCE_LABELS[source],model_key=key,path=path,
                raw_url=raw,params=params,source_url=source_url,media_base_url=media_url)


def readme_sources(canonical,snapshots):
    result=[]
    for snapshot in source_snapshots(canonical,snapshots):
        root=next((str(item.get('path') or item.get('name')) for item in repository_files(snapshot)
            if str(item.get('path') or item.get('name') or '').casefold()=='readme.md'),'README.md')
        target=readme_target(snapshot,root)
        result.append({key:target[key] for key in ['source','source_label','model_key','path','source_url']})
    return result


def fetch_readme(target):
    source=target['source'];label=target['source_label'];response=None
    public={key:target[key] for key in ['source','source_label','path','source_url','media_base_url']}
    try:
        if source=='huggingface':
            from scanners import huggingface as scanner
            scanner._apply_auth();session=scanner.session
        else:
            from scanners import modelscope as scanner
            session=scanner._apply_auth()
        response=scanner.get_with_backoff(session,target['raw_url'],params=target['params'],
            provider=label,label='Collection README',max_retries=1,timeout=(5,15),stream=True)
        status=response.status_code
        if status in (401,403):
            return dict(public,success=False,restricted=True,error=f'This README requires access through your {label} account.'),status
        if status==404:
            return dict(public,success=False,error=f'No README was found on {label} for this repository.'),404
        if status!=200:
            return dict(public,success=False,error=f'{label} returned HTTP {status} for this README.'),502
        content=bytearray();truncated=False
        for chunk in response.iter_content(chunk_size=32*1024):
            if not chunk:continue
            room=MAX_README_BYTES-len(content)
            content.extend(chunk[:room])
            if len(chunk)>room:truncated=True;break
        encoding=response.encoding or 'utf-8'
        try:text=content.decode(encoding,errors='replace')
        except LookupError:text=content.decode('utf-8',errors='replace')
        if ('application/json' in str(response.headers.get('content-type','')).lower()
            or text.lstrip().lower().startswith(('<!doctype html','<html'))):
            return dict(public,success=False,error=f'{label} did not return README text.'),502
        return dict(public,success=True,text=text,truncated=truncated),200
    except Exception as exc:
        logging.warning('Collection README fetch failed: source=%s repository=%s error=%s',source,target['model_key'],type(exc).__name__)
        return dict(public,success=False,error=f'Unable to load this README from {label} right now.'),502
    finally:
        if response is not None:response.close()
