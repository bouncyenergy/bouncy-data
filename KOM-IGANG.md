# Kom igång – steg för steg

Gör detta på en **dator** (inte telefonen). Du behöver: din ENTSO-E-token och zip-filen.
Det tar ca 30–40 minuter första gången. Därefter sköter sig sidan själv.

**Packa upp zip-filen först.** Windows: högerklicka → Extrahera alla. Mac: dubbelklicka. Du får mappen `bouncy-data`.

## A. Skapa konto på GitHub (gratis)
1. Gå till github.com/signup.
2. Fyll i e-post, lösenord och ett användarnamn. Användarnamnet blir en del av sidans adress.
3. Klicka *Create account*, lös pusslet och skriv in koden du får på mejl.

## B. Skapa ett projekt
1. Klicka på **+** uppe till höger → *New repository*.
2. *Repository name*: `bouncy-data`
3. Välj **Public**.
4. Klicka den gröna knappen *Create repository*.

## C. Lägg in din token
1. Klicka fliken **Settings** (längst till höger i raden högst upp i projektet).
2. Till vänster: *Secrets and variables* → *Actions*.
3. Klicka *New repository secret*.
4. *Name*: `ENTSOE_API_KEY` (exakt så).
5. *Secret*: klistra in din token.
6. Klicka *Add secret*.

## D. Slå på webbsidan
1. Fortfarande under Settings: klicka **Pages** i menyn till vänster.
2. Under *Build and deployment* finns en rullista *Source*. Välj **GitHub Actions**.

## E. Ladda upp filerna
1. Klicka fliken **Code**, sedan länken *uploading an existing file*.
2. Öppna mappen `bouncy-data` på datorn. Markera: mapparna `docs` och `data`, samt filerna `update_data.py`, `requirements.txt`, `README.md`, `KOM-IGANG.md`. Dra dem till webbläsarfönstret. (Dra innehållet, inte själva `bouncy-data`-mappen.)
3. Vänta tills de syns i listan. Klicka grön knapp *Commit changes*.

## F. Lägg in uppdateringsflödet
1. På fliken **Code**: *Add file* → *Create new file*.
2. I namnrutan skriver du: `.github/workflows/update.yml`
   Skriv snedstrecken också. Efter varje `/` skapas en mapp.
3. Öppna filen `FLODESFIL-update.yml.txt` (ligger bland de andra filerna) i Anteckningar eller TextEdit. Markera allt (Ctrl+A / Cmd+A), kopiera, och klistra in i den stora textrutan på GitHub.
4. Klicka *Commit changes…* uppe till höger och sedan *Commit changes* i rutan som kommer.

## G. Vänta och öppna sidan
1. Klicka fliken **Actions**. Du ser en körning. Gul snurra = pågår, grön bock = klart, rött kryss = fel.
2. Första gången tar det 10–20 minuter.
3. När det är grönt: **Settings → Pages** visar adressen, som ser ut så här:
   `https://DITT-ANVÄNDARNAMN.github.io/bouncy-data/`

## H. Visa den på Bouncyenergy.com
1. I one.com: skapa en ny undersida, t.ex. "Marknadsdata".
2. Lägg till ett block för inbäddning/HTML/kod och klistra in raden nedan. Byt DITT-ANVÄNDARNAMN.
3. Publicera sajten.

```html
<iframe src="https://DITT-ANVÄNDARNAMN.github.io/bouncy-data/" style="width:100%;height:1100px;border:0" loading="lazy" title="Marknadsdata"></iframe>
```

## Om något går fel
Ta en skärmbild av felet (eller klicka på den röda körningen under Actions, öppna steget med rött kryss och kopiera texten) och skicka till mig.

## Efteråt
* Sidan uppdateras automatiskt varje dag.
* Köra direkt: Actions → *Uppdatera data och publicera* → *Run workflow*.
* Ändra färger: öppna `docs/index.html` på GitHub, klicka pennan, ändra variablerna överst, *Commit changes*.
