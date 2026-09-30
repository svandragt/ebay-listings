// Progressive enhancement: /search/ works without JS (links to eBay); this adds local results.
(function () {
  var q = new URLSearchParams(location.search).get("q") || "";
  var box = document.querySelector('input[name="q"]');
  if (box) box.value = q;
  var out = document.getElementById("results");
  if (!out) return;
  document.querySelectorAll("a.ebay-search").forEach(function (a) {
    a.href += encodeURIComponent(q);
  });
  var words = q.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return;
  fetch("/search.json").then(function (r) { return r.json(); }).then(function (items) {
    var hits = items.filter(function (i) {
      var text = (i.title + " " + i.category).toLowerCase();
      return words.every(function (w) { return text.indexOf(w) !== -1; });
    });
    var p = document.createElement("p");
    p.textContent = hits.length + (hits.length === 1 ? " result" : " results") + " for “" + q + "”";
    var ul = document.createElement("ul");
    ul.className = "grid";
    hits.slice(0, 96).forEach(function (i) {
      var li = document.createElement("li");
      li.className = "h-product";
      var a = document.createElement("a");
      a.href = i.url; a.className = "u-url";
      if (i.image) {
        var img = document.createElement("img");
        img.className = "u-photo"; img.src = i.image; img.srcset = i.srcset; img.sizes = "(max-width: 40rem) 50vw, 11rem"; img.alt = i.title; img.loading = "lazy"; img.width = img.height = 500;
        a.appendChild(img);
      }
      var h = document.createElement("h2");
      h.className = "p-name"; h.textContent = i.title;
      var pr = document.createElement("p");
      pr.className = "p-price"; pr.textContent = i.price;
      a.append(h, pr);
      li.appendChild(a);
      ul.appendChild(li);
    });
    out.replaceChildren(p, ul);
  });
})();
