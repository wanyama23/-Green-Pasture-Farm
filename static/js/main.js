// static/js/main.js
document.addEventListener('DOMContentLoaded', function () {
  // Close navbar on link click (mobile)
  document.querySelectorAll('.navbar-nav .nav-link').forEach(function (link) {
    link.addEventListener('click', function () {
      const bsCollapse = document.querySelector('.navbar-collapse');
      if (bsCollapse && bsCollapse.classList.contains('show')) {
        new bootstrap.Collapse(bsCollapse).hide();
      }
    });
  });

  // Smooth scroll for anchor links (optional)
  document.querySelectorAll('a[href^="#"]').forEach(function (a) {
    a.addEventListener('click', function (e) {
      e.preventDefault();
      const target = document.querySelector(this.getAttribute('href'));
      if (target) target.scrollIntoView({ behavior: 'smooth' });
    });
  });
});
// footer reveal and micro-interactions
document.addEventListener('DOMContentLoaded', function () {
  // IntersectionObserver reveal for .fade-up elements
  const io = new IntersectionObserver((entries, obs) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        entry.target.classList.add('in-view');
        obs.unobserve(entry.target);
      }
    });
  }, { root: null, rootMargin: '0px 0px -8% 0px', threshold: 0.06 });

  document.querySelectorAll('.fade-up').forEach(el => io.observe(el));

  // subtle hover micro-interaction for social icons
  document.querySelectorAll('.social-icons .social-link').forEach(el => {
    el.addEventListener('mouseenter', () => el.style.transform = 'translateY(-4px)');
    el.addEventListener('mouseleave', () => el.style.transform = '');
  });
});
