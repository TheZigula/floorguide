// AnswerText.tsx — renders an answer the way the backend writes it: bold, bullets, and tables.
// Owns: the small slice of Markdown the model actually emits. It builds React elements, never raw HTML.

import type { ReactNode } from "react";

/** Inline: **bold** and `code`. Anything else is left as the literal text the model wrote. */
function inline(text: string, keyPrefix: string): ReactNode[] {
  const out: ReactNode[] = [];
  const pattern = /\*\*(.+?)\*\*|`([^`]+?)`/g;
  let cursor = 0;
  let match: RegExpExecArray | null;
  let n = 0;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > cursor) out.push(text.slice(cursor, match.index));
    if (match[1] !== undefined) {
      out.push(<strong key={keyPrefix + "-b" + n++}>{match[1]}</strong>);
    } else {
      out.push(<code key={keyPrefix + "-c" + n++}>{match[2]}</code>);
    }
    cursor = match.index + match[0].length;
  }
  if (cursor < text.length) out.push(text.slice(cursor));
  return out;
}

const isTableRow = (l: string) => l.trim().startsWith("|");
/** A Markdown separator row: |---|:--:|---| and nothing else. */
const isTableRule = (l: string) => /^\s*\|[\s:|-]+\|\s*$/.test(l);
const isBullet = (l: string) => /^\s*([-*•]|\d+[.)])\s+/.test(l);
const stripBullet = (l: string) => l.replace(/^\s*([-*•]|\d+[.)])\s+/, "");

function cells(row: string): string[] {
  return row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
}

function Block({ lines, k }: { lines: string[]; k: string }) {
  // A table: a header row, a |---| rule, then body rows.
  if (lines.length >= 2 && isTableRow(lines[0]) && isTableRule(lines[1])) {
    const head = cells(lines[0]);
    const body = lines.slice(2).filter(isTableRow).map(cells);
    return (
      <div className="answer__tablewrap">
        <table className="answer__table">
          <thead>
            <tr>
              {head.map((c, i) => (
                <th key={i}>{inline(c, k + "h" + i)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {body.map((row, r) => (
              <tr key={r}>
                {row.map((c, i) => (
                  <td key={i}>{inline(c, k + "r" + r + "c" + i)}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }

  // A list: every line is a bullet or a numbered step.
  if (lines.length > 0 && lines.every(isBullet)) {
    const numbered = /^\s*\d+[.)]\s+/.test(lines[0]);
    const items = lines.map((l, i) => <li key={i}>{inline(stripBullet(l), k + "i" + i)}</li>);
    return numbered ? <ol className="answer__list">{items}</ol> : <ul className="answer__list">{items}</ul>;
  }

  // A heading the model wrote as ### Something.
  if (lines.length === 1 && /^#{1,6}\s+/.test(lines[0])) {
    return <p className="answer__heading">{inline(lines[0].replace(/^#{1,6}\s+/, ""), k + "hd")}</p>;
  }

  // Otherwise a paragraph; single newlines inside it are kept by white-space: pre-wrap.
  return <p>{inline(lines.join("\n"), k + "p")}</p>;
}

export function AnswerText({ text }: { text: string }) {
  const blocks = text
    .split(/\n{2,}/)
    .map((b) => b.replace(/\s+$/, ""))
    .filter((b) => b.trim().length > 0);

  return (
    <>
      {blocks.map((block, i) => (
        <Block key={i} k={"b" + i} lines={block.split("\n")} />
      ))}
    </>
  );
}
