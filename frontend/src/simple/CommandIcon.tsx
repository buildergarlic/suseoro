const paths = {
  add: "M12 5v14M5 12h14",
  upload: "M12 16V3m-5 5 5-5 5 5M4 16v4h16v-4",
  download: "M12 3v13m-5-5 5 5 5-5M4 17v4h16v-4",
  book: "M4 4h6c1 0 2 1 2 2 0-1 1-2 2-2h6v15h-6c-1 0-2 1-2 2 0-1-1-2-2-2H4V4Zm8 2v15",
  list: "M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01",
  settings: "M4 6h16M4 12h16M4 18h16M8 3v6M16 9v6M10 15v6",
} as const;
export function CommandIcon({ name }: { name: keyof typeof paths }) {
  return <svg className="command-icon" width="20" height="20" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d={paths[name]} /></svg>;
}

