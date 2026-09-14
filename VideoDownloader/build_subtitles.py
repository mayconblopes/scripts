"""Build the Portuguese SRT from the locally transcribed Japanese video."""

from __future__ import annotations

import sys
from pathlib import Path


# The timings come from faster-whisper.  Captions are deliberately merged into
# natural Portuguese phrases instead of following every short ASR fragment.
CAPTIONS = [
    (5.68, 11.50, "Olá a todos! Hoje vou apresentar a nova ocarina tripla de plástico Jiegle, em Dó (AC),"),
    (11.50, 17.28, "que acaba de ser lançada. Preparei este vídeo para mostrar o produto a vocês."),
    (17.28, 23.92, "Ué, essa ocarina não já estava à venda há algum tempo?"),
    (23.92, 31.50, "Sim. Antes da pandemia, compramos uma pequena quantidade da Jiegle e a vendemos"),
    (31.50, 39.50, "de forma experimental. Com a boa repercussão e o avanço das vendas, a qualidade também melhorou."),
    (39.50, 49.48, "Por isso, agora transformamos o produto em uma linha oficial e estruturamos a distribuição para lojas de instrumentos do país inteiro."),
    (49.48, 54.32, "Então agora é possível comprá-la em qualquer loja de instrumentos?"),
    (54.32, 59.00, "Isso. Ela estará disponível em lojas de instrumentos de todo o Japão."),
    (59.00, 63.28, "Lojistas, esperamos contar com os seus pedidos."),
    (63.28, 69.12, "E quais são os pontos fortes da ocarina tripla de plástico Jiegle?"),
    (69.12, 78.64, "Comparada a uma ocarina de cerâmica, ela é praticamente igual, inclusive no formato."),
    (78.64, 84.48, "A forma de usar também não muda."),
    (84.48, 88.48, "A digitação e as notas são as mesmas. Quase não há diferença."),
    (88.48, 90.48, "Entendi. Vou experimentar um pouco."),
    (90.48, 91.54, "Claro."),
    (98.11, 100.11, "Impressionante!"),
    (100.11, 102.11, "Ela chega a cerca de três oitavas?"),
    (102.11, 104.11, "Isso, cerca de três oitavas."),
    (104.11, 108.11, "Ela não fica diferente de uma ocarina de cerâmica comum."),
    (108.11, 114.11, "Com uma ocarina tripla de plástico, dá para começar com um preço acessível e se aventurar no instrumento."),
    (114.11, 124.03, "Uma ocarina tripla de cerâmica costuma custar a partir de 80 mil ienes, e existem modelos ainda mais caros."),
    (124.03, 127.67, "É verdade, logo passa dos 100 mil ienes."),
    (127.67, 133.27, "Começar com uma opção acessível facilita muito para quem quer experimentar."),
    (133.27, 140.67, "Quando as ocarinas triplas foram lançadas, eu tinha a impressão de que eram bem difíceis."),
    (140.67, 150.00, "Parecia que só quem tocava havia bastante tempo, já em nível intermediário, conseguia se arriscar."),
    (150.00, 160.27, "Mas hoje até pessoas que estão começando na ocarina querem experimentar uma tripla. Você também percebeu esse aumento?"),
    (160.27, 165.27, "Sim. Eu também trabalho bastante com o TikTok."),
    (165.27, 176.19, "Entre os meus seguidores há muitos jovens, e recebo várias mensagens de pessoas dizendo que querem tocar ocarina tripla."),
    (176.19, 180.83, "Entendi. Mensagens do tipo: “Também quero fazer isso”, certo?"),
    (180.83, 188.43, "Exatamente. Para quem quer tocar uma tripla, um produto assim reduz bastante a barreira de entrada."),
    (188.43, 193.79, "Uma ocarina tripla acessível torna esse desafio muito mais fácil. — É isso mesmo."),
    (193.79, 201.29, "Agora vamos falar das características e dos pontos de venda da ocarina tripla Jiegle, modelo AC."),
    (201.29, 210.09, "E quanto à qualidade? Como ela é?"),
    (210.09, 220.09, "A Jiegle é uma empresa chinesa. Nós importamos as ocarinas da China,"),
    (220.09, 229.77, "mas um professor de ocarina chinês parceiro da Night verifica cada unidade individualmente."),
    (229.77, 234.89, "Então um professor especializado confere tudo antes do envio? — Isso."),
    (234.89, 245.25, "Depois que chegam ao Japão, a equipe da Night também verifica a afinação e, quando necessário, faz pequenos ajustes."),
    (245.25, 252.73, "Como temos esse sistema de inspeção, acredito que os clientes podem comprar com tranquilidade."),
    (252.73, 258.23, "Então não há motivo para preocupação com a qualidade? — Exatamente."),
    (258.23, 263.45, "E as cores? São estas duas? — Isso. Dá para ver?"),
    (263.45, 269.00, "Temos o modelo marfim e o preto."),
    (281.64, 286.88, "Na verdade, a pintura e o acabamento das cores são feitos pela Night."),
    (286.88, 291.20, "Ah, vocês fazem isso na Night. Por que decidiram pintar aqui?"),
    (291.20, 298.48, "A pintura chinesa não é ruim, mas, olhando de perto, encontramos algumas áreas irregulares."),
    (298.48, 303.72, "Por isso, decidimos fazer o acabamento nós mesmos, com bastante cuidado."),
    (303.72, 314.00, "Se houver alguma parte defeituosa, o cliente acaba prejudicado e pode ficar inseguro. Dá bastante trabalho, mas vale a pena."),
    (314.00, 325.04, "Além disso, a tinta que usamos no Japão atende aos padrões de segurança da Lei de Higiene Alimentar."),
    (325.04, 331.68, "É uma tinta que pode até ser usada em utensílios de mesa, então todos podem usar a ocarina com tranquilidade."),
    (331.68, 336.00, "Então a segurança também está garantida. — Sim, é segura."),
    (337.00, 346.02, "Há outros pontos de venda? — Sim. Esta ocarina tripla já vem com uma alça."),
    (346.02, 353.52, "Uma alça? — Isso, uma alça com cordão em espiral, feita especialmente para ela."),
    (353.52, 362.50, "Na parte elástica há uma mola espiral sob medida."),
    (362.50, 374.40, "Com essa mola, o cordão não fica caindo sobre os controles nem sobre o bocal, o que facilita tocar."),
    (374.40, 387.68, "Assim, ele não fica balançando na frente do bocal. Se isso acontecesse, poderia até atrapalhar bastante o som."),
    (387.68, 395.56, "Por isso, colocamos uma mola exclusiva para reduzir esse tipo de problema durante a execução."),
    (395.56, 406.80, "A fivela da alça também foi projetada para segurança: quando recebe uma força forte, ela se solta."),
    (406.80, 416.16, "Onde fica? — Aqui, nesta peça vermelha. Está conseguindo ver?"),
    (416.16, 421.68, "A fivela se solta quando é aplicada uma força forte o suficiente."),
    (421.68, 433.68, "Isso evita acidentes: se alguém puxar a alça por engano e ela não se soltasse, poderia apertar o pescoço da pessoa."),
    (433.68, 441.12, "Para evitar esse risco, ela foi feita para se desprender. — Entendi."),
    (441.12, 452.00, "Mas, se ela se solta, não existe o risco de a ocarina cair e quebrar?"),
    (452.00, 460.76, "Entendo a preocupação, mas ela não se solta com uma força fraca. A segurança da pessoa vem em primeiro lugar."),
    (460.76, 471.08, "Claro. E, depois disso, também projetamos a alça para que a ocarina não se danifique facilmente."),
    (471.08, 476.08, "Ela é bem resistente; na maioria das situações, não haverá problema."),
    (476.08, 487.08, "Há outros pontos de venda? — Ainda há vários. Este modelo vem com um manual de instruções,"),
    (487.08, 493.08, "que inclui duas músicas para praticar. — Então é este material aqui? — Isso."),
    (493.08, 500.06, "As músicas são “Annie Laurie” e “Haru no Ogawa”. Não são muito difíceis,"),
    (500.06, 510.60, "mas passam por diferentes oitavas, permitindo praticar usando o primeiro, o segundo e o terceiro tubo da ocarina tripla."),
    (510.60, 519.84, "Além disso, há um código QR com os acompanhamentos, para que você possa ouvir como as músicas devem ser tocadas."),
    (519.84, 527.50, "E quem fez essas gravações de demonstração? — Fui eu."),
    (527.50, 541.18, "O Tom fez as gravações de demonstração. Ao adquirir o produto, vocês podem acessar o código QR do manual:"),
    (541.18, 547.52, "os vídeos ficam disponíveis gratuitamente no YouTube para praticar acompanhando a execução do Tom."),
    (586.23, 595.63, "Há mais algum ponto de venda? — Sim. Esta ocarina tripla também vem com um estojo."),
    (595.63, 601.91, "É um estojo bem firme e resistente, próprio para proteger o instrumento."),
    (601.91, 612.35, "E, como vimos no começo, apesar de ser uma ocarina tripla de plástico, o som é muito bom, não é?"),
    (612.35, 621.67, "Sim. Mesmo sendo de plástico, ela produz um som realmente muito bom e torna a prática divertida."),
    (621.67, 627.15, "Essa é a parte mais importante. Quanto custa?"),
    (627.15, 634.75, "A ocarina tripla de plástico inclui a alça, o estojo e o conjunto de material didático."),
    (634.75, 637.07, "O preço é de 12 mil ienes, sem impostos."),
    (637.07, 643.03, "Incrível! Com esse preço, muita gente vai poder começar, não é? — Com certeza."),
    (643.03, 652.21, "Espero que todos usem esta ocarina tripla de plástico acessível para aceitar o desafio de tocar uma tripla."),
    (652.21, 660.07, "Experimentem! E vocês também, esforcem-se e divirtam-se aprendendo a tocar ocarina tripla. Tchau!"),
]


def timestamp(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds_value, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds_value:02d},{millis:03d}"


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] in {"-h", "--help"}:
        print("uso: build_subtitles.py [SAIDA_SRT]")
        return 0
    destination = Path(sys.argv[1]) if len(sys.argv) == 2 else Path("Jiegle_Triple_Ocarina_pt-BR.srt")
    lines: list[str] = []
    for index, (start, end, text) in enumerate(CAPTIONS, start=1):
        lines.extend([str(index), f"{timestamp(start)} --> {timestamp(end)}", text, ""])
    destination.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print(f"{len(CAPTIONS)} legendas gravadas em {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
