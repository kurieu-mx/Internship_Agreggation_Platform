# Your resume lives here

Drop **one** file in this directory named `master.<ext>`. It is the source of
truth for the *format* of every tailored resume the pipeline produces.

## Why a source file and not the PDF

The pipeline never edits a PDF. It regenerates one from structured content
through the same renderer that produced your master, which is what makes every
tailored variant come out with byte-identical layout. Editing PDF text in place
cannot do that — the moment a replacement string is a different width, the line
breaks move and the page reflows.

So the format fidelity you get is decided entirely by what you put here:

| File you provide | Renderer | Fidelity |
|---|---|---|
| `master.tex` (or an Overleaf export) | `tectonic` / `latexmk` | **Exact** — same compiler, same output |
| `master.docx` | LibreOffice headless | **Exact** — real Word rendering, styles untouched |
| `master.html` + CSS | WeasyPrint | High, but hand-matched by eye |

`.tex` is the best option if you have it. `.docx` is just as faithful and
needs no LaTeX install. HTML is the fallback when the original source is gone
and the layout has to be rebuilt from a PDF.

## What happens next

Running the setup step reads `master.<ext>` and writes `profile.yml` beside it:
a structured version of your content — every bullet you have ever written,
grouped by role, plus projects and skills.

From then on the two files have different jobs, and the split is what keeps the
tailoring honest:

- **`master.<ext>` is the format.** Never rewritten by the pipeline.
- **`profile.yml` is the content pool.** Tailoring selects, reorders, and
  rewords entries *from this pool*. A generated bullet that does not trace back
  to an entry here fails validation and the run falls back to your untailored
  master for that company.

That is the mechanism that stops a model from inventing experience you do not
have and putting it on a resume with your name at the top.

Edit `profile.yml` freely — it is meant to be curated. Adding a bullet there
makes it available for tailoring; deleting one takes it out of circulation.

## Not committed

This directory is git-ignored apart from this README. Your resume, phone
number, and address stay on your machine and in your CI secrets, not in a
public repo.
