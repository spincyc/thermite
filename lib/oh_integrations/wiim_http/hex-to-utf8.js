// Decodes the hex-encoded UTF-8 strings the WiiM (LinkPlay) HTTP API returns
// for Title, Artist and Album. Input that is not even-length hex is passed
// through unchanged, so an already-plain value still displays.
(function (hex) {
  if (typeof hex !== 'string' || hex.length === 0 || hex.length % 2 !== 0 || /[^0-9a-fA-F]/.test(hex)) {
    return hex;
  }
  try {
    return decodeURIComponent(hex.replace(/../g, '%$&'));
  } catch (e) {
    return hex;
  }
})(input)
