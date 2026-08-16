/* Chirp public site — shared vanilla JS (no framework, no build step). */
(function () {
  "use strict";

  var toggle = document.getElementById("nav-toggle");
  var nav = document.getElementById("site-nav");
  if (!toggle || !nav) return;

  function closeNav() {
    nav.classList.remove("open");
    toggle.setAttribute("aria-expanded", "false");
  }

  function openNav() {
    nav.classList.add("open");
    toggle.setAttribute("aria-expanded", "true");
  }

  toggle.addEventListener("click", function () {
    var expanded = toggle.getAttribute("aria-expanded") === "true";
    if (expanded) {
      closeNav();
    } else {
      openNav();
    }
  });

  nav.querySelectorAll("a").forEach(function (link) {
    link.addEventListener("click", closeNav);
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") closeNav();
  });

  document.addEventListener("click", function (event) {
    if (!nav.classList.contains("open")) return;
    if (nav.contains(event.target) || toggle.contains(event.target)) return;
    closeNav();
  });
})();
