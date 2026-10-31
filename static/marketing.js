(() => {
    const button = document.querySelector(".marketing-menu-button");
    const nav = document.querySelector(".marketing-nav");
    if (!button || !nav) return;
    const setOpen = (open) => {
        button.setAttribute("aria-expanded", String(open));
        nav.classList.toggle("is-open", open);
    };
    button.addEventListener("click", () => setOpen(button.getAttribute("aria-expanded") !== "true"));
    nav.querySelectorAll("a").forEach((link) => link.addEventListener("click", () => setOpen(false)));
})();
