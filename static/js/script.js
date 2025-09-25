document.addEventListener("DOMContentLoaded", () => {
    const forms = document.querySelectorAll("form[data-loading]");

    forms.forEach((form) => {
        form.addEventListener("submit", () => {
            const submitButton = form.querySelector("button[type='submit']");
            if (submitButton) {
                submitButton.disabled = true;
                submitButton.dataset.originalText = submitButton.textContent;
                submitButton.textContent = "Working…";
            }
        });
    });
});
