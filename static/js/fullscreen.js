function initializeFullscreen(){


    const overlay =
        document.getElementById("imageOverlay");


    const closeButton =
        document.getElementById("closeImage");


    if(!overlay || !closeButton){

        return;

    }



    closeButton.addEventListener(
        "click",
        closeFullscreen
    );


    const fullscreenImage = document.getElementById("fullscreenImage");
    if(fullscreenImage){
        fullscreenImage.addEventListener("dblclick", event => {
            event.preventDefault();
            closeFullscreen();
        });
    }



    overlay.addEventListener(
        "click",
        event => {


            if(event.target === overlay){

                closeFullscreen();

            }


        }
    );



    document.addEventListener(
        "keydown",
        event => {


            if(event.key === "Escape"){

                closeFullscreen();

            }


        }
    );


}



function closeFullscreen(){


    const overlay =
        document.getElementById("imageOverlay");


    const image =
        document.getElementById("fullscreenImage");



    if(!overlay){

        return;

    }



    overlay.classList.remove(
        "open"
    );



    if(image){

        image.src = "";

    }


}