import json
from pathlib import Path


CONFIG_PATH = (
    Path(__file__)
    .parent
    .parent
    / "app_config"
)


def load_config(filename):

    path = CONFIG_PATH / filename

    if not path.exists():
        return {}

    with open(
        path,
        "r",
        encoding="utf-8"
    ) as file:

        return json.load(file)



def save_config(filename, data):

    path = CONFIG_PATH / filename

    with open(
        path,
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            data,
            file,
            indent=4
        )



def load_architectures():

    return load_config(
        "architectures.json"
    )



def load_model_types():

    return load_config(
        "model_types.json"
    )



def load_preferences():

    return load_config(
        "preferences.json"
    )