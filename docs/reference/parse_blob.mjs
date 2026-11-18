import fs from "node:fs"; import v8 from "node:v8"; import { uncompress } from "snappyjs";
const raw = fs.readFileSync(process.argv[2]);
let d = raw;
if (raw[0]===0xff && raw[1]===0x11 && raw[2]===0x02) d = Buffer.from(uncompress(raw.subarray(3)));
console.log("decoded header:", [...d.subarray(0,20)].map(b=>b.toString(16).padStart(2,"0")).join(" "));
// find inner V8 header (ff 0f/ff 10) – Blink v21 envelope puts it at offset 15
let off = -1; for (let i=0;i<64;i++) if (d[i]===0xff && (d[i+1]===0x0f||d[i+1]===0x10) ) { off=i; if (i>0) break; }
const p = Buffer.from(d.subarray(off)); if (p[1]===0x10) p[1]=0x0f;
const v = v8.deserialize(p);
console.log("offset", off, "channels", Object.keys(v.channels), "msgs", Object.keys(v.messages.C0GUEST1).length);
