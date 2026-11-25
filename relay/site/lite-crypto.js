/* Talos Relay: AES-GCM, SHA-256 and HMAC-SHA-256 written out, for a page opened over plain http.

   A browser gives a page its own cipher (crypto.subtle) only over https or on the computer itself.
   The relay inside Talos (talos relay mode local) hands the page out over plain http on your own
   network, and there the page seals with this file instead. It takes the same calls and gives the
   same results; tests/test_relay.py checks it against the cipher of Node and of the computer side.

   The random numbers still come from the browser (crypto.getRandomValues works on plain http).
   This cipher is not hardened against a program that measures its timing on the same phone, so
   the page uses the browser's own cipher wherever the browser offers one.
*/
"use strict";
(function (root) {
  function bytesFrom(data) {
    if (data instanceof Uint8Array) return data;
    if (ArrayBuffer.isView(data)) return new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
    return new Uint8Array(data);
  }
  function nameOf(algorithm) { return typeof algorithm === "string" ? algorithm : algorithm && algorithm.name; }
  function attempt(work) { return new Promise(function (resolve) { resolve(work()); }); }

  // ------------------------------------------------------------ SHA-256 (FIPS 180-4)
  var K = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2]);

  function sha256(message) {
    var length = message.length, total = (length + 9 + 63) & ~63;
    var padded = new Uint8Array(total), view = new DataView(padded.buffer);
    padded.set(message);
    padded[length] = 0x80;
    view.setUint32(total - 8, Math.floor(length / 0x20000000));
    view.setUint32(total - 4, (length << 3) >>> 0);
    var h = new Uint32Array([0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]);
    var w = new Uint32Array(64);
    for (var at = 0; at < total; at += 64) {
      var i;
      for (i = 0; i < 16; i += 1) w[i] = view.getUint32(at + i * 4);
      for (i = 16; i < 64; i += 1) {
        var x = w[i - 15], y = w[i - 2];
        var s0 = ((x >>> 7) | (x << 25)) ^ ((x >>> 18) | (x << 14)) ^ (x >>> 3);
        var s1 = ((y >>> 17) | (y << 15)) ^ ((y >>> 19) | (y << 13)) ^ (y >>> 10);
        w[i] = (w[i - 16] + s0 + w[i - 7] + s1) | 0;
      }
      var a = h[0], b = h[1], c = h[2], d = h[3], e = h[4], f = h[5], g = h[6], hh = h[7];
      for (i = 0; i < 64; i += 1) {
        var big1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7));
        var t1 = (hh + big1 + ((e & f) ^ (~e & g)) + K[i] + w[i]) | 0;
        var big0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10));
        var t2 = (big0 + ((a & b) ^ (a & c) ^ (b & c))) | 0;
        hh = g; g = f; f = e; e = (d + t1) | 0;
        d = c; c = b; b = a; a = (t1 + t2) | 0;
      }
      h[0] += a; h[1] += b; h[2] += c; h[3] += d; h[4] += e; h[5] += f; h[6] += g; h[7] += hh;
    }
    var out = new Uint8Array(32), made = new DataView(out.buffer);
    for (var n = 0; n < 8; n += 1) made.setUint32(n * 4, h[n]);
    return out;
  }

  // ------------------------------------------------------------ HMAC-SHA-256 (RFC 2104)
  function hmac(key, message) {
    if (key.length > 64) key = sha256(key);
    var inner = new Uint8Array(64 + message.length), outer = new Uint8Array(96);
    for (var i = 0; i < 64; i += 1) {
      var part = i < key.length ? key[i] : 0;
      inner[i] = part ^ 0x36;
      outer[i] = part ^ 0x5c;
    }
    inner.set(message, 64);
    outer.set(sha256(inner), 64);
    return sha256(outer);
  }

  // ------------------------------------------------------------ AES, encryption only (FIPS 197)
  var SBOX = new Uint8Array(256);
  var T0 = new Uint32Array(256), T1 = new Uint32Array(256), T2 = new Uint32Array(256), T3 = new Uint32Array(256);
  (function tables() {
    var p = 1, q = 1;
    do {
      p = (p ^ (p << 1) ^ (p & 0x80 ? 0x1b : 0)) & 0xff;            // p times 3
      q = (q ^ (q << 1)) & 0xff;                                      // q divided by 3
      q = (q ^ (q << 2)) & 0xff;
      q = (q ^ (q << 4)) & 0xff;
      if (q & 0x80) q ^= 0x09;
      var turned = q ^ ((q << 1) | (q >>> 7)) ^ ((q << 2) | (q >>> 6)) ^ ((q << 3) | (q >>> 5)) ^ ((q << 4) | (q >>> 4));
      SBOX[p] = (turned ^ 0x63) & 0xff;
    } while (p !== 1);
    SBOX[0] = 0x63;
    for (var x = 0; x < 256; x += 1) {
      var s = SBOX[x], s2 = ((s << 1) ^ (s & 0x80 ? 0x11b : 0)) & 0xff, s3 = s2 ^ s;
      T0[x] = ((s2 << 24) | (s << 16) | (s << 8) | s3) >>> 0;
      T1[x] = ((s3 << 24) | (s2 << 16) | (s << 8) | s) >>> 0;
      T2[x] = ((s << 24) | (s3 << 16) | (s2 << 8) | s) >>> 0;
      T3[x] = ((s << 24) | (s << 16) | (s3 << 8) | s2) >>> 0;
    }
  }());

  function subWord(word) {
    return ((SBOX[word >>> 24] << 24) | (SBOX[(word >>> 16) & 255] << 16) | (SBOX[(word >>> 8) & 255] << 8) | SBOX[word & 255]) >>> 0;
  }
  function expand(key) {
    var words = key.length / 4;
    if (words !== 4 && words !== 6 && words !== 8) throw new Error("An AES key is 16, 24 or 32 bytes long.");
    var view = new DataView(key.buffer, key.byteOffset, key.byteLength);
    var round = new Uint32Array(4 * (words + 7)), constant = 1;
    for (var i = 0; i < words; i += 1) round[i] = view.getUint32(i * 4);
    for (i = words; i < round.length; i += 1) {
      var word = round[i - 1];
      if (i % words === 0) {
        word = subWord(((word << 8) | (word >>> 24)) >>> 0) ^ (constant << 24);
        constant = ((constant << 1) ^ (constant & 0x80 ? 0x11b : 0)) & 0xff;
      } else if (words > 6 && i % words === 4) {
        word = subWord(word);
      }
      round[i] = (round[i - words] ^ word) >>> 0;
    }
    return round;
  }
  /* One block: the four words of `from` sealed into the four words of `to`. */
  function block(round, from, to) {
    var s0 = from[0] ^ round[0], s1 = from[1] ^ round[1], s2 = from[2] ^ round[2], s3 = from[3] ^ round[3];
    var last = round.length - 4, t0, t1, t2, t3;
    for (var at = 4; at < last; at += 4) {
      t0 = T0[s0 >>> 24] ^ T1[(s1 >>> 16) & 255] ^ T2[(s2 >>> 8) & 255] ^ T3[s3 & 255] ^ round[at];
      t1 = T0[s1 >>> 24] ^ T1[(s2 >>> 16) & 255] ^ T2[(s3 >>> 8) & 255] ^ T3[s0 & 255] ^ round[at + 1];
      t2 = T0[s2 >>> 24] ^ T1[(s3 >>> 16) & 255] ^ T2[(s0 >>> 8) & 255] ^ T3[s1 & 255] ^ round[at + 2];
      t3 = T0[s3 >>> 24] ^ T1[(s0 >>> 16) & 255] ^ T2[(s1 >>> 8) & 255] ^ T3[s2 & 255] ^ round[at + 3];
      s0 = t0; s1 = t1; s2 = t2; s3 = t3;
    }
    to[0] = ((SBOX[s0 >>> 24] << 24) | (SBOX[(s1 >>> 16) & 255] << 16) | (SBOX[(s2 >>> 8) & 255] << 8) | SBOX[s3 & 255]) ^ round[last];
    to[1] = ((SBOX[s1 >>> 24] << 24) | (SBOX[(s2 >>> 16) & 255] << 16) | (SBOX[(s3 >>> 8) & 255] << 8) | SBOX[s0 & 255]) ^ round[last + 1];
    to[2] = ((SBOX[s2 >>> 24] << 24) | (SBOX[(s3 >>> 16) & 255] << 16) | (SBOX[(s0 >>> 8) & 255] << 8) | SBOX[s1 & 255]) ^ round[last + 2];
    to[3] = ((SBOX[s3 >>> 24] << 24) | (SBOX[(s0 >>> 16) & 255] << 16) | (SBOX[(s1 >>> 8) & 255] << 8) | SBOX[s2 & 255]) ^ round[last + 3];
  }

  // ------------------------------------------------------------ GCM (NIST SP 800-38D), 12-byte nonce, 16-byte tag
  /* y = y times h in the field of GCM, bit by bit. */
  function multiply(y, h) {
    var z0 = 0, z1 = 0, z2 = 0, z3 = 0, v0 = h[0], v1 = h[1], v2 = h[2], v3 = h[3];
    for (var i = 0; i < 128; i += 1) {
      if ((y[i >>> 5] >>> (31 - (i & 31))) & 1) { z0 ^= v0; z1 ^= v1; z2 ^= v2; z3 ^= v3; }
      var carry = v3 & 1;
      v3 = (v3 >>> 1) | (v2 << 31);
      v2 = (v2 >>> 1) | (v1 << 31);
      v1 = (v1 >>> 1) | (v0 << 31);
      v0 = v0 >>> 1;
      if (carry) v0 ^= 0xe1000000;
    }
    y[0] = z0; y[1] = z1; y[2] = z2; y[3] = z3;
  }
  function absorb(y, h, bytes) {
    for (var at = 0; at < bytes.length; at += 16) {
      for (var i = 0; i < 16 && at + i < bytes.length; i += 1) y[i >> 2] ^= bytes[at + i] << (24 - 8 * (i & 3));
      multiply(y, h);
    }
  }
  function tagOf(round, start, extra, sealed) {
    var h = new Uint32Array(4), y = new Uint32Array(4), mask = new Uint32Array(4);
    block(round, h, h);
    absorb(y, h, extra);
    absorb(y, h, sealed);
    y[0] ^= Math.floor(extra.length / 0x20000000);
    y[1] ^= extra.length << 3;
    y[2] ^= Math.floor(sealed.length / 0x20000000);
    y[3] ^= sealed.length << 3;
    multiply(y, h);
    block(round, start, mask);
    var tag = new Uint8Array(16), view = new DataView(tag.buffer);
    for (var i = 0; i < 4; i += 1) view.setUint32(i * 4, (y[i] ^ mask[i]) >>> 0);
    return tag;
  }
  function stream(round, start, from, to) {
    var counter = new Uint32Array([start[0], start[1], start[2], 2]), pad = new Uint32Array(4);
    for (var at = 0; at < from.length; at += 16) {
      block(round, counter, pad);
      counter[3] += 1;
      for (var i = 0; i < 16 && at + i < from.length; i += 1) to[at + i] = from[at + i] ^ ((pad[i >> 2] >>> (24 - 8 * (i & 3))) & 255);
    }
  }
  function gcm(key, algorithm, data, sealing) {
    if (!key || key.kind !== "AES-GCM") throw new Error("This is not an AES-GCM key.");
    var nonce = bytesFrom(algorithm.iv), extra = algorithm.additionalData ? bytesFrom(algorithm.additionalData) : new Uint8Array(0);
    if (nonce.length !== 12) throw new Error("This cipher takes a 12-byte nonce.");
    var view = new DataView(nonce.buffer, nonce.byteOffset, 12);
    var start = new Uint32Array([view.getUint32(0), view.getUint32(4), view.getUint32(8), 1]);
    if (sealing) {
      var sealed = new Uint8Array(data.length + 16);
      stream(key.round, start, data, sealed.subarray(0, data.length));
      sealed.set(tagOf(key.round, start, extra, sealed.subarray(0, data.length)), data.length);
      return sealed.buffer;
    }
    if (data.length < 16) throw new Error("This was not sealed with this key.");
    var body = data.subarray(0, data.length - 16), tag = tagOf(key.round, start, extra, body), differs = 0;
    for (var i = 0; i < 16; i += 1) differs |= tag[i] ^ data[body.length + i];
    if (differs) throw new Error("This was not sealed with this key.");
    var plain = new Uint8Array(body.length);
    stream(key.round, start, body, plain);
    return plain.buffer;
  }

  // ------------------------------------------------------------ the calls the page makes, as crypto.subtle answers them
  var lite = {
    importKey: function (format, raw, algorithm) {
      return attempt(function () {
        var bytes = new Uint8Array(bytesFrom(raw)), name = nameOf(algorithm);       // a copy: the caller may reuse its bytes
        if (format !== "raw") throw new Error("Only a raw key is read.");
        if (name === "AES-GCM") return { kind: "AES-GCM", round: expand(bytes) };
        if (name === "HMAC" && nameOf(algorithm.hash) === "SHA-256") return { kind: "HMAC", raw: bytes };
        throw new Error("Only AES-GCM and HMAC with SHA-256 are written out here.");
      });
    },
    encrypt: function (algorithm, key, data) {
      return attempt(function () { return gcm(key, algorithm, bytesFrom(data), true); });
    },
    decrypt: function (algorithm, key, data) {
      return attempt(function () { return gcm(key, algorithm, bytesFrom(data), false); });
    },
    digest: function (algorithm, data) {
      return attempt(function () {
        if (nameOf(algorithm) !== "SHA-256") throw new Error("Only SHA-256 is written out here.");
        return sha256(bytesFrom(data)).buffer;
      });
    },
    sign: function (algorithm, key, data) {
      return attempt(function () {
        if (nameOf(algorithm) !== "HMAC" || !key || key.kind !== "HMAC") throw new Error("This is not an HMAC key.");
        return hmac(key.raw, bytesFrom(data)).buffer;
      });
    },
  };
  if (typeof module === "object" && module.exports) module.exports = lite;
  else root.TalosLiteCrypto = lite;
}(typeof self !== "undefined" ? self : this));
