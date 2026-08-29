interface BudgetStripProps {
  budgetWon: number;
  quoteWon: number | null;
}

const won = new Intl.NumberFormat("ko-KR");

export function BudgetStrip({ budgetWon, quoteWon }: BudgetStripProps) {
  const difference = quoteWon === null ? null : budgetWon - quoteWon;
  return (
    <dl className="budget-strip" aria-label="승인 예산과 선택 견적">
      <div><dt>승인 예산</dt><dd>{won.format(budgetWon)}원</dd></div>
      <div><dt>선택 견적</dt><dd>{quoteWon === null ? "아직 선택하지 않음" : `${won.format(quoteWon)}원`}</dd></div>
      <div><dt>차이</dt><dd>{difference === null ? "—" : difference >= 0 ? `${won.format(difference)}원 남음` : `${won.format(Math.abs(difference))}원 초과`}</dd></div>
    </dl>
  );
}
