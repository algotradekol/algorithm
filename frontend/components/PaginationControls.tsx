'use client';

export const PAGE_SIZE = 15;

export function PaginationControls({
  page,
  totalRows,
  onPageChange,
}: {
  page: number;
  totalRows: number;
  onPageChange: (page: number) => void;
}) {
  const pageCount = Math.max(1, Math.ceil(totalRows / PAGE_SIZE));
  const safePage = Math.min(page, pageCount - 1);

  return (
    <div className="mt-3 flex items-center justify-between gap-3 text-xs text-gray-500">
      <span>Showing {totalRows ? safePage * PAGE_SIZE + 1 : 0}-{Math.min((safePage + 1) * PAGE_SIZE, totalRows)} of {totalRows}</span>
      <div className="flex gap-2">
        <button
          type="button"
          disabled={safePage === 0}
          onClick={() => onPageChange(Math.max(0, safePage - 1))}
          className="min-h-10 rounded border border-[#1f2937] px-3 py-1 disabled:opacity-40"
        >
          Previous
        </button>
        <button
          type="button"
          disabled={safePage >= pageCount - 1}
          onClick={() => onPageChange(Math.min(pageCount - 1, safePage + 1))}
          className="min-h-10 rounded border border-[#1f2937] px-3 py-1 disabled:opacity-40"
        >
          Next
        </button>
      </div>
    </div>
  );
}
