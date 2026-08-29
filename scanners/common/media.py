def extract_media(
    files,
    base_url
):

    preview = ""
    preview_count = 0
    has_video = False
    media = []

    position = 0


    for file in files:

        if isinstance(file, dict):
            filename = file.get("path", file.get("name", ""))
            # Display media should use the plain resolve URL. Download URLs can
            # carry attachment-oriented query parameters such as ?download=true.
            url = file.get("media_url") or file.get("download_url", "")
        else:
            filename = str(file)
            url = ""

        if not filename:
            continue

        name = filename.lower()

        if not url:
            url = f"{base_url}/{filename}"


        if name.endswith(
            (
                ".png",
                ".jpg",
                ".jpeg",
                ".webp"
            )
        ):

            preview_count += 1


            if not preview:
                preview = url


            media.append({
                "type":"image",
                "url":url,
                "thumbnail":"",
                "filename": filename.rsplit("/", 1)[-1],
                "path": filename,
                "metadata": {
                    "filename": filename.rsplit("/", 1)[-1],
                    "path": filename
                },
                "position":position
            })


            position += 1



        elif name.endswith(
            (
                ".mp4",
                ".webm",
                ".mov"
            )
        ):

            has_video = True


            media.append({
                "type":"video",
                "url":url,
                "thumbnail":"",
                "filename": filename.rsplit("/", 1)[-1],
                "path": filename,
                "metadata": {
                    "filename": filename.rsplit("/", 1)[-1],
                    "path": filename
                },
                "position":position
            })


            position += 1



    return {
        "image":preview,
        "preview_count":preview_count,
        "has_video":has_video,
        "has_media":bool(media),
        "media":media
    }