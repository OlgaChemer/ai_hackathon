// Представление чисел для синтеза речи. Не извлекает пожелания и не меняет суммы в данных.
const ONES = ["", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"];
const TEENS = ["десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать", "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать"];
const TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят", "девяносто"];
const HUNDREDS = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот", "девятьсот"];
const ONES_GEN = ["", "одного", "двух", "трёх", "четырёх", "пяти", "шести", "семи", "восьми", "девяти"];
const TENS_GEN = ["", "", "двадцати", "тридцати", "сорока", "пятидесяти", "шестидесяти", "семидесяти", "восьмидесяти", "девяноста"];
const HUNDREDS_GEN = ["", "ста", "двухсот", "трёхсот", "четырёхсот", "пятисот", "шестисот", "семисот", "восьмисот", "девятисот"];
const SCALES = [null, ["тысяча", "тысячи", "тысяч"], ["миллион", "миллиона", "миллионов"], ["миллиард", "миллиарда", "миллиардов"], ["триллион", "триллиона", "триллионов"]];
const FRACTIONS = [["десятая", "десятых"], ["сотая", "сотых"], ["тысячная", "тысячных"], ["десятитысячная", "десятитысячных"], ["стотысячная", "стотысячных"], ["миллионная", "миллионных"]];

function form(value, words) {
  const n = Number(BigInt(value) % 100n);
  return words[n % 10 === 1 && n !== 11 ? 0 : n % 10 >= 2 && n % 10 <= 4 && (n < 12 || n > 14) ? 1 : 2];
}

export function integerWords(value, feminine = false, genitive = false) {
  let n = BigInt(value);
  if (n < 0n) return `минус ${integerWords(-n, feminine, genitive)}`;
  if (!n) return genitive ? "нуля" : "ноль";
  if (n >= 1000n ** BigInt(SCALES.length)) return String(value);
  const groups = [];
  for (let scale = 0; n; scale++, n /= 1000n) {
    const part = Number(n % 1000n);
    if (!part) continue;
    const words = [(genitive ? HUNDREDS_GEN : HUNDREDS)[Math.floor(part / 100)]];
    const tail = part % 100;
    if (tail >= 10 && tail < 20) words.push(genitive ? TEENS[tail - 10].replace(/ь$/, "и") : TEENS[tail - 10]);
    else {
      words.push((genitive ? TENS_GEN : TENS)[Math.floor(tail / 10)]);
      const unit = tail % 10;
      const female = scale === 1 || (scale === 0 && feminine);
      words.push(genitive ? (female && unit === 1 ? "одной" : ONES_GEN[unit]) : female && unit <= 2 ? ["", "одна", "две"][unit] : ONES[unit]);
    }
    if (scale) {
      const names = SCALES[scale];
      words.push(genitive ? form(part, [names[1], names[2], names[2]]) : form(part, names));
    }
    groups.unshift(words.filter(Boolean).join(" "));
  }
  return groups.join(" ");
}

function decimal(value, power = 0) {
  const [whole, fraction = ""] = value.replace(/[−-\s]/g, "").replace(",", ".").split(".");
  const digits = (whole + fraction).replace(/^0+(?=\d)/, "");
  const places = Math.max(0, fraction.length - power);
  return { coefficient: BigInt(digits) * 10n ** BigInt(Math.max(0, power - fraction.length)), places, negative: /^[−-]/.test(value) };
}

function decimalWords(number, genitive = false) {
  const { coefficient, negative } = number;
  let places = number.places;
  let digits = coefficient;
  while (places && digits % 10n === 0n) { digits /= 10n; places--; }
  if (!places) return (negative ? "минус " : "") + integerWords(digits, false, genitive);
  const divider = 10n ** BigInt(places);
  const whole = digits / divider, fraction = digits % divider;
  const denominator = FRACTIONS[places - 1];
  if (!denominator) return `${negative ? "минус " : ""}${integerWords(whole, false, genitive)} запятая ${String(fraction).padStart(places, "0").split("").map(d => d === "0" ? "ноль" : ONES[Number(d)]).join(" ")}`;
  return `${negative ? "минус " : ""}${integerWords(whole, true, genitive)} ${form(whole, [genitive ? "целой" : "целая", "целых", "целых"])} ${integerWords(fraction, true, genitive)} ${form(fraction, [genitive ? denominator[0].replace(/ая$/, "ой") : denominator[0], denominator[1], denominator[1]])}`;
}

function quantity(value, scale, unit, genitive = false) {
  const power = /^млрд/i.test(scale) ? 9 : /^млн/i.test(scale) ? 6 : /^тыс/i.test(scale) ? 3 : 0;
  const n = decimal(value, power);
  const divider = 10n ** BigInt(n.places);
  const integral = n.coefficient % divider === 0n;
  const whole = n.coefficient / divider;
  if (/^(₽|руб)/i.test(unit)) {
    const kopecks = n.coefficient % divider * 100n;
    if (kopecks % divider === 0n) {
      const rest = kopecks / divider;
      return `${n.negative ? "минус " : ""}${integerWords(whole, false, genitive)} ${form(whole, genitive ? ["рубля", "рублей", "рублей"] : ["рубль", "рубля", "рублей"])}${rest ? ` ${integerWords(rest, true, genitive)} ${form(rest, genitive ? ["копейки", "копеек", "копеек"] : ["копейка", "копейки", "копеек"])}` : ""}`;
    }
    return `${decimalWords(n, genitive)} рубля`;
  }
  const words = integral ? `${n.negative ? "минус " : ""}${integerWords(whole, /^комнат/i.test(unit), genitive)}` : decimalWords(n, genitive);
  if (/^комнат/i.test(unit)) return `${words} ${integral ? form(whole, genitive ? ["комнаты", "комнат", "комнат"] : ["комната", "комнаты", "комнат"]) : "комнаты"}`;
  if (unit === "%") return `${words} ${integral ? form(whole, genitive ? ["процента", "процентов", "процентов"] : ["процент", "процента", "процентов"]) : "процента"}`;
  if (/^(м²|м2|кв)/i.test(unit)) return `${words} ${integral ? form(whole, genitive ? ["квадратного метра", "квадратных метров", "квадратных метров"] : ["квадратный метр", "квадратных метра", "квадратных метров"]) : "квадратного метра"}`;
  return words;
}

export function roomDescription(rooms) {
  const values = [...new Set(rooms.filter(n => Number.isInteger(n) && n >= 0 && n <= 20))];
  const parts = values.map(n => n === 0 ? "студия" : `${integerWords(n, true)} ${form(n, ["комната", "комнаты", "комнат"])}`);
  return parts.length < 2 ? parts.join("") : `${parts.slice(0, -1).join(", ")} или ${parts.at(-1)}`;
}

const NUMBER = "[−-]?(?:\\d{1,3}(?:[ \u00a0\u202f]\\d{3})+|\\d+)(?:[.,]\\d+)?";
const SCALE = "млрд\\.?|млн\\.?|тыс\\.?";
const UNIT = "комнат(?:а|ы)?|₽|руб(?:лей|ля|ль|\\.)?|%|м²|м2|кв\\.\\s*м\\.?";

function genitiveContext(source, offset) {
  return /(?:^|\s)(?:до|от|около|выше|ниже|больше|меньше|не более|не менее)\s*$/iu.test(source.slice(0, offset));
}

function sentenceDot(match, offset, source) {
  return match.endsWith(".") && /^(?:\s*$|\s+[А-ЯЁA-Z])/.test(source.slice(offset + match.length)) ? "." : "";
}

export function speechText(text) {
  let result = String(text)
    .replace(/\[([^\]]+)\]\(https?:\/\/[^)]+\)/g, "$1")
    .replace(/https?:\/\/\S+/g, "ссылка на экране")
    .replace(/(^|\n)\s*#{1,6}\s+/g, "$1")
    .replace(/[*`_]/g, "")
    .replace(/≈/g, "около ").replace(/≤/g, "не больше ").replace(/≥/g, "не меньше ");
  // Общая единица диапазона относится к обеим границам: «8–10 млн ₽».
  result = result.replace(new RegExp(`(^|[^\\p{L}\\d])(${NUMBER})\\s*(?:[–—-]|до)\\s*(${NUMBER})\\s*(${SCALE})?\\s*(${UNIT})?(?!\\p{L})`, "giu"),
    (match, before, from, to, scale = "", unit = "", offset, source) => {
      if (!scale && !unit) return match;
      return `${before}${genitiveContext(source, offset + before.length) ? "" : "от "}${quantity(from, scale, unit, true)} до ${quantity(to, scale, unit, true)}${sentenceDot(match, offset, source)}`;
    });
  result = result.replace(new RegExp(`(^|[^\\p{L}\\d])(${NUMBER})\\s*(${SCALE})?\\s*(${UNIT})(?!\\p{L})`, "giu"),
    (match, before, value, scale = "", unit, offset, source) => before + quantity(value, scale, unit, genitiveContext(source, offset + before.length)) + sentenceDot(match, offset, source));
  result = result.replace(new RegExp(`(^|[^\\p{L}\\d])(${NUMBER})\\s*(${SCALE})(?!\\p{L})`, "giu"),
    (match, before, value, scale, offset, source) => before + quantity(value, scale, "", genitiveContext(source, offset + before.length)) + sentenceDot(match, offset, source));
  result = result.replace(/(^|[^\p{L}\d])([−-]?\d{1,3}(?:[ \u00a0\u202f]\d{3})+)(?!\d|[ \u00a0\u202f]\d)/gu,
    (_, before, value, offset, source) => before + decimalWords(decimal(value), genitiveContext(source, offset + before.length)));
  return result
    .replace(/(^|\s)ИИ(?=[\s,.!?-]|$)/g, "$1искусственный интеллект")
    .replace(/СБР/g, "сервис безопасных расчётов")
    .replace(/ЕГРН/g, "Е Г Р Н")
    .replace(/₽/g, "рублей")
    .replace(/\/\s*(?:месяц|мес\.?)(?!\p{L})/giu, " в месяц")
    .replace(/(^|\s)ул\.\s*/giu, "$1улица ")
    .replace(/[→←↑↓•·✓✕«»"]/g, " ")
    .replace(/\p{Extended_Pictographic}/gu, "")
    .replace(/\s+/g, " ").trim();
}

export function speechChunks(text, limit = 220) {
  const clean = speechText(text);
  const sentences = [];
  let start = 0;
  for (let i = 0; i < clean.length; i++) {
    if (/[.!?…]/.test(clean[i]) && (i + 1 === clean.length || /\s/.test(clean[i + 1]))) {
      sentences.push(clean.slice(start, i + 1).trim()); start = i + 1;
    }
  }
  if (clean.slice(start).trim()) sentences.push(clean.slice(start).trim());
  const chunks = [];
  let current = "";
  for (const sentence of sentences) {
    if (current && current.length + sentence.length + 1 > limit) { chunks.push(current); current = ""; }
    if (sentence.length <= limit) {
      current += (current ? " " : "") + sentence;
      continue;
    }
    // Длинное предложение делим по словам; обычные предложения сохраняем целиком.
    for (const word of sentence.split(/\s+/)) {
      if (current && current.length + word.length + 1 > limit) { chunks.push(current); current = ""; }
      current += (current ? " " : "") + word;
    }
  }
  if (current) chunks.push(current);
  return chunks;
}
