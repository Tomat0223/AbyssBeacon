import json
import scanner


def run_scan(
    selected_sources,
    search_terms=None,
    selected_architecture="",
    selected_architectures=None
):


    with open("scan_terms.json", "w") as f:

        json.dump(
            search_terms,
            f
        )


    result = scanner.run_scan(
        selected_sources,
        search_terms,
        selected_architecture,
        selected_architectures=selected_architectures
    )


    return result