/** ISBN links are searches; provider record identifiers are never invented. */
export function canonicalIsbn(value: string): string | null {
  const digits = value.normalize("NFKC").replace(/[\s-]/g, "").toUpperCase();
  if (/^\d{9}[\dX]$/.test(digits)) {
    const sum = [...digits].reduce((n, digit, i) => n + (digit === "X" ? 10 : Number(digit)) * (10 - i), 0);
    if (sum % 11) return null;
    const prefix = "978" + digits.slice(0, 9);
    const check = (10 - [...prefix].reduce((n, digit, i) => n + Number(digit) * (i % 2 ? 3 : 1), 0) % 10) % 10;
    return prefix + check;
  }
  if (!/^97[89]\d{10}$/.test(digits)) return null;
  return [...digits].reduce((n, digit, i) => n + Number(digit) * (i % 2 ? 3 : 1), 0) % 10 === 0 ? digits : null;
}
