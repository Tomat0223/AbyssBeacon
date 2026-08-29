function initializeSettings() {

    const checkbox = document.getElementById("hideNoMedia");

    if (!checkbox) {
        return;
    }


    checkbox.addEventListener(
        "change",
        () => {

            fetch("/save_preferences", {
                method:"POST",
                headers:{
                    "Content-Type":"application/json"
                },
                body:JSON.stringify({
                    show_media_only: checkbox.checked
                })
            });

            filterCards();

        }
    );

}