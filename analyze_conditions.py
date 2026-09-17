"""分析温度、车速、闪充与新增 AFE 误码及七类报警的关系。"""
from analysis_common import analysis_parser, fit_models, load_analysis, new_output


def main():
    args = analysis_parser(__doc__).parse_args()
    out = new_output(args.output)
    data, _ = load_analysis(args.cache, out, max_rows=args.max_model_rows, min_coverage=args.min_coverage)
    fit_models(data, out, min_vins=args.min_vins, min_coverage=args.min_coverage)


if __name__ == "__main__":
    main()
