function initializeCards() {

    document.querySelectorAll(".model-card").forEach(card => {

        card.addEventListener("click", () => {

            const badge = card.querySelector(".badge-new");

            if (badge) {
                badge.remove();
            }

            card.dataset.status = "seen";

        });

    });

}