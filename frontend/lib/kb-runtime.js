(function (global) {
  var origin = global.location.origin;
  global.KB_RUNTIME = Object.freeze({
    apiBase: global.__KB_API_BASE__ || origin + "/api/v1",
    baseUrl: origin,
  });
})(window);
