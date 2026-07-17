// Fuzzy street-name matching, shared by index.html (the game) and matchtest.html (the tester).
// core(name) -> a comparison key: lowercased, de-accented, numbers spelled out in French,
// punctuation dropped, leading generic words (rue/place/de/…) stripped. lev() is edit distance.
const STRIP = new Set(("rue ruelle avenue place placette passage villa allee impasse boulevard bd square route "+
  "chemin cite acces quai quaie cour cours pont sortie port promenade galerie sentier sente hameau esplanade "+
  "parvis jardin jardins mail rond point carrefour pont "+
  "de du des la le les l d aux au a en et sur").split(" "));
const U=["","un","deux","trois","quatre","cinq","six","sept","huit","neuf","dix","onze","douze","treize",
  "quatorze","quinze","seize","dix-sept","dix-huit","dix-neuf"];
function b100(n){ if(n<20)return U[n]; const t=(n/10)|0,u=n%10;
  if(t===7||t===9)return (t===7?"soixante":"quatre-vingt")+"-"+U[10+u];
  const T=["","","vingt","trente","quarante","cinquante","soixante","","quatre-vingt"][t];
  if(u===0)return t===8?"quatre-vingts":T;
  if(u===1&&t!==8)return T+"-et-un";
  return T+"-"+U[u]; }
function b1000(n){ if(n<100)return b100(n); const c=(n/100)|0,r=n%100;
  const p=c===1?"cent":U[c]+"-cent"; return r?p+"-"+b100(r):(c===1?"cent":U[c]+"-cents"); }
function frCard(n){ if(n===0)return "zero"; const t=(n/1000)|0,r=n%1000;
  return (t?(t===1?"mille":b1000(t)+"-mille"):"")+(r?(t?"-":"")+b1000(r):""); }
function frOrd(n){ if(n===1)return "premier"; let w=frCard(n);
  return (w.endsWith("e")?w.slice(0,-1):w.endsWith("f")?w.slice(0,-1)+"v":w.endsWith("q")?w+"u":w)+"ieme"; }
function spellNums(s){ return s.replace(/(\d+)(ers?|res?|iemes?|emes?|es?)?\b/g,
  (m,d,suf)=> " "+(suf?frOrd(+d):frCard(+d))+" "); }
// superscript/subscript letters & digits (e.g. "Iᵉʳ" -> "Ier") so ordinals normalize like plain text
const SUP={"ᵃ":"a","ᵇ":"b","ᶜ":"c","ᵈ":"d","ᵉ":"e","ᶠ":"f","ᵍ":"g","ʰ":"h","ⁱ":"i","ʲ":"j","ᵏ":"k",
  "ˡ":"l","ᵐ":"m","ⁿ":"n","ᵒ":"o","ᵖ":"p","ʳ":"r","ˢ":"s","ᵗ":"t","ᵘ":"u","ᵛ":"v","ʷ":"w","ˣ":"x",
  "ʸ":"y","ᶻ":"z","⁰":"0","¹":"1","²":"2","³":"3","⁴":"4","⁵":"5","⁶":"6","⁷":"7","⁸":"8","⁹":"9",
  "₀":"0","₁":"1","₂":"2","₃":"3","₄":"4","₅":"5","₆":"6","₇":"7","₈":"8","₉":"9"};
const SUP_RX=new RegExp("["+Object.keys(SUP).join("")+"]","g");
const RVAL={I:1,V:5,X:10};                                    // real Paris roman numerals only use I,V,X
function roman(r){ let n=0,p=0; for(let i=r.length-1;i>=0;i--){ const v=RVAL[r[i]]; n+=v<p?-v:v; p=v; } return n; }
// convert roman numerals -> digits. Uppercase-only (so lowercase de/le/du and words like "vélodrome"
// don't match) and only Ier/Ire (the sole roman ordinal in the data). The (?!\/) skips codes like
// "V/11","X/13". Assumes superscripts already expanded and accents stripped; run before lowercasing.
function deRoman(s){ return s.replace(/\b[IVX]+(er|re)?\b(?!\/)/g,
  (m,suf)=> " "+(suf?frOrd(roman(m.slice(0,-suf.length))):roman(m))+" "); }
function core(s){
  let x = s.replace(SUP_RX,c=>SUP[c])            // expand superscripts (Iᵉʳ -> Ier) BEFORE NFD, which
          .normalize("NFD").replace(/\p{Diacritic}/gu,"");  // would otherwise decompose & drop them
  x = deRoman(x).toLowerCase();                  // roman on de-accented text so \b is reliable
  let t = spellNums(x).replace(/[^a-z0-9]+/g," ").trim()
           .replace(/\bst\b/g,"saint").replace(/\bste\b/g,"sainte").split(" ");
  while (t.length > 1 && STRIP.has(t[0])) t.shift();
  return t.join("");
}
function lev(a,b){
  const m=a.length,n=b.length; if(!m)return n; if(!n)return m;
  let p=Array.from({length:n+1},(_,i)=>i);
  for(let i=1;i<=m;i++){ let prev=p[0]; p[0]=i;
    for(let j=1;j<=n;j++){ const t=p[j]; p[j]=Math.min(p[j]+1,p[j-1]+1,prev+(a[i-1]===b[j-1]?0:1)); prev=t; } }
  return p[n];
}
