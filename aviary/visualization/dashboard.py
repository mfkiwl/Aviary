import argparse
import json
import os
from pathlib import Path
import pathlib
import shutil
import importlib.util
from string import Template
from dataclasses import dataclass
from typing import (
    List,
    Iterator,
    Tuple,
)  # Use typing.List and typing.Tuple for compatibility

import numpy as np
from bokeh.palettes import Category10
import hvplot.pandas  # noqa # need this ! Otherwise hvplot using DataFrames does not work
import pandas as pd
import panel as pn
from panel.theme import DefaultTheme

import openmdao.api as om
from openmdao.utils.general_utils import env_truthy
try:
    from openmdao.utils.gui_testing_utils import get_free_port
except:
    # If get_free_port is unavailable, the default port will be used
    def get_free_port():
        return 5000
from openmdao.utils.om_warnings import issue_warning

from aviary.visualization.aircraft_3d_model import Aircraft3DModel

# support getting this function from OpenMDAO post movement of the function to utils
#    but also support its old location
try:
    from openmdao.utils.array_utils import convert_ndarray_to_support_nans_in_json
except ImportError:
    from openmdao.visualization.n2_viewer.n2_viewer import (
        _convert_ndarray_to_support_nans_in_json as convert_ndarray_to_support_nans_in_json,
    )

import aviary.api as av

pn.extension(sizing_mode="stretch_width")
pn.extension('tabulator')


# Constants
aviary_variables_json_file_name = "aviary_vars.json"
documentation_text_align = 'left'

# functions for the aviary command line command


def _none_or_str(value):
    """
    Get the value of the argparse option.

    If "None", return None. Else, just return the string.

    Parameters
    ----------
    value : str
        The value used by the user on the command line for the argument.

    Returns
    -------
    option_value : str or None
        The value of the option after possibly converting from 'None' to None.
    """
    if value == "None":
        return None
    return value


def _dashboard_setup_parser(parser):
    """
    Set up the aviary subparser for the 'aviary dashboard' command.

    Parameters
    ----------
    parser : argparse subparser
        The parser we're adding options to.
    """
    parser.add_argument(
        "script_name",
        type=str,
        help="Name of aviary script that was run (not including .py).",
    )

    parser.add_argument(
        "--problem_recorder",
        type=str,
        help="Problem case recorder file name",
        dest="problem_recorder",
        default="problem_history.db",
    )
    parser.add_argument(
        "--driver_recorder",
        type=_none_or_str,
        help="Driver case recorder file name. Set to None if file is ignored",
        dest="driver_recorder",
        default="driver_history.db",
    )
    parser.add_argument(
        "--port",
        dest="port",
        type=int,
        default=0,
        help="dashboard server port ID (default is 0, which indicates get any free port)",
    )

    # For future use
    parser.add_argument(
        "-d",
        "--debug",
        action="store_true",
        dest="debug_output",
        help="show debugging output",
    )


def _dashboard_cmd(options, user_args):
    """
    Run the dashboard command.

    Parameters
    ----------
    options : argparse Namespace
        Command line options.
    user_args : list of str
        Args to be passed to the user script.
    """
    dashboard(
        options.script_name,
        options.problem_recorder,
        options.driver_recorder,
        options.port,
    )


# functions for creating Panel Panes given different kinds of
#    data inputs
def create_csv_frame(csv_filepath, documentation):
    """
    Create a Panel Pane that contains a tabular display of the data in a CSV file.

    Parameters
    ----------
    csv_filepath : str
        Path to the input CSV file.
    documentation : str
        Explanation of what this tab is showing.

    Returns
    -------
    pane : Panel.Pane or None
        A Panel Pane object showing the tabular display of the CSV file contents. 
        Or None if the CSV file does not exist.
    """
    if os.path.exists(csv_filepath):
        df = pd.read_csv(csv_filepath)
        df_pane = pn.widgets.Tabulator(
            df,
            show_index=False,
            sortable=False,
            layout="fit_data_stretch",
            max_height=600,
            sizing_mode='scale_both',
        )
        report_pane = pn.Column(
            pn.pane.HTML(f"<p>{documentation}</p>",
                         styles={'text-align': documentation_text_align}),
            df_pane
        )
    else:
        report_pane = None

    return report_pane


def create_report_frame(format, text_filepath, documentation):
    """
    Create a Panel Pane that contains an embedded external file in HTML, Markdown, or text format.

    Parameters
    ----------
    format : str
        Format of the file to be embedded. Options are 'html', 'text', 'markdown'.
    text_file_name : str
        Name of the report text file.
    documentation : str
        Explanation of what this tab is showing.

    Returns
    -------
    pane : Panel.Pane or None
        A Panel Pane object to be displayed in the dashboard. Or None if the file
        does not exist.
    """
    if os.path.exists(text_filepath):
        if format == "html":
            iframe_css = 'width=1200px height=800px overflow-x="scroll" overflow="scroll" margin=0px padding=0px border=20px frameBorder=20px scrolling="yes"'
            report_pane = pn.pane.HTML(
                f"<p>{documentation}</p><iframe {iframe_css} src=/home/{text_filepath}></iframe>"
            )
            report_pane = pn.Column(
                pn.pane.HTML(f"<p>{documentation}</p>",
                             styles={'text-align': documentation_text_align}),
                pn.pane.HTML(f"<iframe {iframe_css} src=/home/{text_filepath}></iframe>")
            )
        elif format in ["markdown", "text"]:
            with open(text_filepath, "rb") as f:
                file_text = f.read()
                # need to deal with some encoding errors
                file_text = file_text.decode("latin-1")
            if format == "markdown":
                report_pane = pn.pane.Markdown(file_text)
            elif format == "text":
                report_pane = pn.pane.Markdown(f"```\n{file_text}\n```\n")
            report_pane = pn.Column(
                pn.pane.HTML(f"<p>{documentation}</p>",
                             styles={'text-align': documentation_text_align}),
                report_pane
            )

        else:
            raise RuntimeError(f"Report format of {format} is not supported.")
    else:
        report_pane = None
    return report_pane


def create_aviary_variables_table_data_nested(script_name, recorder_file):
    """
    Create a JSON file with information about Aviary variables.

    The JSON file has one level of hierarchy of the variables. The file
    is written to aviary_vars.json. That file is then read in by the
    aviary/visualization/assets/aviary_vars/script.js script. That is inside the
    aviary/visualization/assets/aviary_vars/index.html file that is embedded in the
    dashboard.

    The information about the variables comes from a case recorder file.

    Parameters
    ----------
    recorder_file : str
        Name of the recorder file containing the Problem cases.

    Returns
    -------
    table_data_nested
        A nested list of information about the Aviary variables.

    """
    cr = om.CaseReader(recorder_file)

    if "final" not in cr.list_cases():
        return None

    case = cr.get_case("final")
    outputs = case.list_outputs(
        explicit=True,
        implicit=True,
        val=True,
        residuals=True,
        residuals_tol=None,
        units=True,
        shape=True,
        bounds=True,
        desc=True,
        scaling=False,
        hierarchical=True,
        print_arrays=True,
        out_stream=None,
        return_format="dict",
    )

    sorted_abs_names = sorted(outputs.keys())

    grouped = {}
    for s in sorted_abs_names:
        prefix = s.split(":")[0]
        if prefix not in grouped:
            grouped[prefix] = []
        grouped[prefix].append(s)

    sorted_group_names = sorted(grouped.keys())

    table_data_nested = []
    for group_name in sorted_group_names:
        if len(grouped[group_name]) == 1:  # a list of one var.
            var_info = grouped[group_name][0]
            prom_name = outputs[var_info]["prom_name"]
            aviary_metadata = av.CoreMetaData.get(prom_name)
            table_data_nested.append(
                {
                    "abs_name": group_name,
                    "prom_name": prom_name,
                    "value": convert_ndarray_to_support_nans_in_json(
                        outputs[var_info]["val"]
                    ),
                    "units": outputs[var_info]["units"],
                    "metadata": json.dumps(aviary_metadata),
                }
            )
        else:
            # create children
            children_list = []
            for children_name in grouped[group_name]:
                prom_name = outputs[children_name]["prom_name"]
                aviary_metadata = av.CoreMetaData.get(prom_name)
                children_list.append(
                    {
                        "abs_name": children_name,
                        "prom_name": prom_name,
                        "value": convert_ndarray_to_support_nans_in_json(
                            outputs[children_name]["val"]
                        ),
                        "units": outputs[children_name]["units"],
                        "metadata": json.dumps(aviary_metadata),
                    }
                )
            table_data_nested.append(  # not a real var, just a group of vars so no values
                {
                    "abs_name": group_name,
                    "prom_name": "",
                    "value": "",
                    "units": "",
                    "_children": children_list,
                }
            )

    aviary_variables_file_path = (
        f"reports/{script_name}/aviary_vars/{aviary_variables_json_file_name}"
    )
    with open(aviary_variables_file_path, "w") as fp:
        json.dump(table_data_nested, fp)

    return table_data_nested


def convert_case_recorder_file_to_df(recorder_file_name):
    """
    Convert a case recorder file into a Pandas data frame.

    Parameters
    ----------
    recorder_file_name : str
        Name of the case recorder file.
    """
    cr = om.CaseReader(recorder_file_name)
    driver_cases = cr.list_cases("driver", out_stream=None)

    df = None
    for i, case in enumerate(driver_cases):
        driver_case = cr.get_case(case)

        desvars = driver_case.get_design_vars(scaled=False)
        objectives = driver_case.get_objectives(scaled=False)
        constraints = driver_case.get_constraints(scaled=False)

        if i == 0:  # Only need to get header of the data frame once
            # Need to worry about the fact that a variable can be in more than one of
            #  desvars, cons, and obj. So filter out the dupes
            initial_desvars_names = list(desvars.keys())
            initial_constraints_names = list(constraints.keys())
            objectives_names = list(objectives.keys())

            # Start with obj, then cons, then desvars
            # Give priority to having a duplicate being in the obj and cons
            #  over being in the desvars
            all_var_names = objectives_names.copy()
            constraints_names = []
            for name in initial_constraints_names:
                if name not in all_var_names:
                    constraints_names.append(name)
                    all_var_names.append(name)
            desvars_names = []
            for name in initial_desvars_names:
                if name not in all_var_names:
                    desvars_names.append(name)
                    all_var_names.append(name)
            header = ["iter_count"] + all_var_names
            df = pd.DataFrame(columns=header)

        # Now fill up a row
        row = [
            i,
        ]
        # important to do in this order since that is the order added to the header
        for varname in objectives_names:
            value = objectives[varname]
            if not np.isscalar(value):
                value = np.linalg.norm(value)
            row.append(value)

        for varname in constraints_names:
            value = constraints[varname]
            if not np.isscalar(value):
                value = np.linalg.norm(value)
            row.append(value)

        for varname in desvars_names:
            value = desvars[varname]
            if not np.isscalar(value):
                value = np.linalg.norm(value)
            row.append(value)
        df.loc[i] = row

    return df


def create_aircraft_3d_file(recorder_file, reports_dir, outfilepath):
    """
    Create the HTML file with the display of the aircraft design
    in 3D using the A-Frame library.

    Parameters
    ----------
    recorder_file : str
        Name of the case recorder file.
    reports_dir : str
        Path of the directory containing the reports from the run.
    outfilepath : str
        The path to the location where the file should be created.
    """
    # Get the location of the HTML template file for this HTML file
    aviary_dir = pathlib.Path(importlib.util.find_spec("aviary").origin).parent
    aircraft_3d_template_filepath = aviary_dir.joinpath(
        "visualization/assets/aircraft_3d_file_template.html"
    )

    # texture for the aircraft. Need to copy it to the reports directory
    #  next to the HTML file
    shutil.copy(
        aviary_dir.joinpath("visualization/assets/aviary_airlines.png"),
        f"{reports_dir}/aviary_airlines.png",
    )

    aircraft_3d_model = Aircraft3DModel(recorder_file)

    aircraft_3d_model.write_file(aircraft_3d_template_filepath, outfilepath)


# The main script that generates all the tabs in the dashboard
def dashboard(script_name, problem_recorder, driver_recorder, port):
    """
    Generate the dashboard app display.

    Parameters
    ----------
    script_name : str
        Name of the script file whose results will be displayed by this dashboard.
    problem_recorder : str
        Name of the recorder file containing the Problem cases.
    driver_recorder : str or None
        Name of the recorder file containing the Driver cases. If None, the driver tab will not be added
    port : int
        HTTP port used for the dashboard webapp. If 0, use any free port
    """
    reports_dir = f"reports/{script_name}/"

    if not Path(reports_dir).is_dir():
        raise ValueError(
            f"The script name, '{script_name}', does not have a reports folder associated with it. "
            f"The directory '{reports_dir}' does not exist."
        )

    # TODO - use lists and functions to do this with a lot less code
    ####### Model Tab #######
    model_tabs_list = []

    #  Debug Input List
    input_list_pane = create_report_frame("text", "input_list.txt", '''
       A plain text display of the model inputs. Recommended for beginners. Only created if debug_mode is set to True in the input deck.
        The variables are listed in a tree structure. There are three columns. The left column is a list of variable names,
        the middle column is the value, and the right column is the 
        promoted variable name. The hierarchy is phase, subgroups, components, and variables. An input variable can appear under 
        different phases and within different components. Its values can be different because its value has 
        been updated during the computation. On the top-left corner is the total number of inputs. 
        That number counts the duplicates because one variable can appear in different phases.''')
    if input_list_pane:
        model_tabs_list.append(("Debug Input List", input_list_pane))

    #  Debug Output List
    output_list_pane = create_report_frame("text", "output_list.txt", '''
       A plain text display of the model outputs. Recommended for beginners. Only created if debug_mode is set to True in the input deck.
        The variables are listed in a tree structure. There are three columns. The left column is a list of variable names,
        the middle column is the value, and the right column is the 
        promoted variable name. The hierarchy is phase, subgroups, components, and variables. An output variable can appear under 
        different phases and within different components. Its values can be different because its value has 
        been updated during the computation. On the top-left corner is the total number of outputs. 
        That number counts the duplicates because one variable can appear in different phases.''')
    if output_list_pane:
        model_tabs_list.append(("Debug Output List", output_list_pane))

    # Inputs
    inputs_pane = create_report_frame(
        "html", f"{reports_dir}/inputs.html", "Detailed report on the model inputs.")
    if inputs_pane:
        model_tabs_list.append(("Inputs", inputs_pane))

    # N2
    n2_pane = create_report_frame("html", f"{reports_dir}/n2.html", '''
        The N2 diagram, sometimes referred to as an eXtended Design Structure Matrix (XDSM), is a 
        powerful tool for understanding your model in OpenMDAO. It is an N-squared diagram in the 
        shape of a matrix representing functional or physical interfaces between system elements. 
        It can be used to systematically identify, define, tabulate, design, and analyze functional 
        and physical interfaces.''')
    if n2_pane:
        model_tabs_list.append(("N2", n2_pane))

    # Trajectory Linkage
    traj_linkage_report_pane = create_report_frame(
        "html", f"{reports_dir}/traj_linkage_report.html", '''
        This is a Dymos linkage report in a customized N2 diagram. It provides a report detailing how phases 
        are linked together via constraint or connection. The diagram clearly shows how mission phases are linked.
        It can be used to identify errant linkages between fixed quantities.
        '''
    )
    if traj_linkage_report_pane:
        model_tabs_list.append(("Trajectory Linkage", traj_linkage_report_pane))

    ####### Optimization Tab #######
    optimization_tabs_list = []

    # Driver scaling
    driver_scaling_report_pane = create_report_frame(
        "html", f"{reports_dir}/driver_scaling_report.html", '''
            This report is a summary of driver scaling information. After all design variables, objectives, and constraints 
            are declared and the problem has been set up, this report presents all the design variables and constraints 
            in all phases as well as the objectives. It also shows Jacobian information showing responses with respect to 
            design variables (DV).
        '''
    )
    if driver_scaling_report_pane:
        optimization_tabs_list.append(
            ("Driver Scaling", driver_scaling_report_pane)
        )

    # Desvars, cons, opt interactive plot
    if driver_recorder:
        if os.path.exists(driver_recorder):
            df = convert_case_recorder_file_to_df(f"{driver_recorder}")
            if df is not None:
                variables = pn.widgets.CheckBoxGroup(
                    name="Variables",
                    options=list(df.columns),
                    # just so all of them aren't plotted from the beginning. Skip the iter count
                    value=list(df.columns)[1:2],
                )
                ipipeline = df.interactive()
                ihvplot = ipipeline.hvplot(
                    y=variables,
                    responsive=True,
                    min_height=400,
                    color=list(Category10[10]),
                    yformatter="%.0f",
                    title="Model Optimization using OpenMDAO",
                )
                optimization_plot_pane = pn.Column(
                    pn.Row(
                        pn.Column(
                            variables,
                            pn.VSpacer(height=30),
                            pn.VSpacer(height=30),
                            width=300,
                        ),
                        ihvplot.panel(),
                    )
                )
            else:
                optimization_plot_pane = pn.pane.Markdown(
                    f"# Recorder file '{driver_recorder}' does not have Driver case recordings."
                )
        else:
            optimization_plot_pane = pn.pane.Markdown(
                f"# Recorder file '{driver_recorder}' not found.")

        optimization_plot_pane_with_doc = pn.Column(
            pn.pane.HTML(f"<p>Plot of design variables, constraints, and objectives.</p>",
                         styles={'text-align': documentation_text_align}),
            optimization_plot_pane
        )
        optimization_tabs_list.append(
            ("History", optimization_plot_pane_with_doc)
        )

    # IPOPT report
    ipopt_pane = create_report_frame("text", f"{reports_dir}/IPOPT.out", '''
        This report is generated by the IPOPT optimizer.
                                     ''')
    if ipopt_pane:
        optimization_tabs_list.append(("IPOPT Output", ipopt_pane))

    # Optimization report
    opt_report_pane = create_report_frame("html", f"{reports_dir}/opt_report.html", '''
        This report is an OpenMDAO optimization report. All values are in unscaled, physical units. 
        On the top is a summary of the optimization, followed by the objective, design variables, constraints, 
        and optimizer settings. This report is important when dissecting optimal results produced by Aviary.''')
    if opt_report_pane:
        optimization_tabs_list.append(("Summary", opt_report_pane))

    # PyOpt report
    pyopt_solution_pane = create_report_frame(
        "text", f"{reports_dir}/pyopt_solution.txt", '''
         This report is generated by the pyOptSparse optimizer.
       '''
    )
    if pyopt_solution_pane:
        optimization_tabs_list.append(("PyOpt Solution", pyopt_solution_pane))

    # SNOPT report
    snopt_pane = create_report_frame("text", f"{reports_dir}/SNOPT_print.out", '''
        This report is generated by the SNOPT optimizer.
                                     ''')
    if snopt_pane:
        optimization_tabs_list.append(("SNOPT Output", snopt_pane))

    # SNOPT summary
    snopt_summary_pane = create_report_frame("text", f"{reports_dir}/SNOPT_summary.out", '''
        This is a report generated by the SNOPT optimizer that summarizes the optimization results.''')
    if snopt_summary_pane:
        optimization_tabs_list.append(("SNOPT Summary", snopt_summary_pane))

    # Coloring report
    coloring_report_pane = create_report_frame(
        "html", f"{reports_dir}/total_coloring.html", "The report shows metadata associated with the creation of the coloring."
    )
    if coloring_report_pane:
        optimization_tabs_list.append(("Total Coloring", coloring_report_pane))

    ####### Results Tab #######
    results_tabs_list = []

    # Aircraft 3d model display
    if problem_recorder:
        if os.path.exists(problem_recorder):

            try:
                create_aircraft_3d_file(
                    problem_recorder, reports_dir, f"{reports_dir}/aircraft_3d.html"
                )
                aircraft_3d_pane = create_report_frame(
                    "html", f"{reports_dir}/aircraft_3d.html",
                    "3D model view of designed aircraft."
                )
                if aircraft_3d_pane:
                    results_tabs_list.append(("Aircraft 3d model", aircraft_3d_pane))
            except Exception as e:
                issue_warning(
                    f"Unable to create aircraft 3D model display due to error {e}"
                )

    # Make the Aviary variables table pane
    if os.path.exists(problem_recorder):

        # Make dir reports/script_name/aviary_vars if needed
        aviary_vars_dir = pathlib.Path(f"reports/{script_name}/aviary_vars")
        aviary_vars_dir.mkdir(parents=True, exist_ok=True)

        # copy index.html file to reports/script_name/aviary_vars/index.html
        aviary_dir = pathlib.Path(importlib.util.find_spec("aviary").origin).parent

        shutil.copy(
            aviary_dir.joinpath("visualization/assets/aviary_vars/index.html"),
            aviary_vars_dir.joinpath("index.html"),
        )
        shutil.copy(
            aviary_dir.joinpath("visualization/assets/aviary_vars/script.js"),
            aviary_vars_dir.joinpath("script.js"),
        )
        # copy script.js file to reports/script_name/aviary_vars/index.html.
        # mod the script.js file to point at the json file
        # create the json file and put it in reports/script_name/aviary_vars/aviary_vars.json
        try:
            create_aviary_variables_table_data_nested(
                script_name, problem_recorder
            )  # create the json file

            aviary_vars_pane = create_report_frame(
                "html", f"{reports_dir}/aviary_vars/index.html",
                "Table showing Aviary variables"
            )
            results_tabs_list.append(("Aviary Variables", aviary_vars_pane))
        except Exception as e:
            issue_warning(
                f"Unable do create Aviary Variables tab in dashboard due to the error: {str(e)}"
            )

    # Mission Summary
    mission_summary_pane = create_report_frame(
        "markdown", f"{reports_dir}/mission_summary.md", "A report of mission results from an Aviary problem.")
    if mission_summary_pane:
        results_tabs_list.append(("Mission Summary", mission_summary_pane))

    # Trajectory results
    traj_results_report_pane = create_report_frame(
        "html", f"{reports_dir}/traj_results_report.html", '''
            This is one of the most important reports produced by Aviary. It will help you visualize and 
            understand the optimal trajectory produced by Aviary.
            Users should play with it and try to grasp all possible features. 
            This report contains timeseries and phase parameters in different tabs. 
            On the timeseries tab, users can select which phases to view. 
            Other features include hovering the mouse over the solution points to see solution value and 
            zooming into a particular region for details, etc.
        '''
    )
    if traj_results_report_pane:
        results_tabs_list.append(
            ("Trajectory Results", traj_results_report_pane)
        )

    # Timeseries Mission Output Report
    mission_timeseries_pane = create_csv_frame(
        f"{reports_dir}/mission_timeseries_data.csv", '''
        The outputs of the aircraft trajectory.
        Any value that is included in the timeseries data is included in this report.
        This data is useful for post-processing, especially those used for acoustic analysis.
        ''')
    if mission_timeseries_pane:
        results_tabs_list.append(
            ("Timeseries Mission Output", mission_timeseries_pane)
        )

    ####### Subsystems Tab #######
    subsystem_tabs_list = []

    # Look through subsystems directory for markdown files
    # The subsystems report tab shows selected results for every major subsystem in the Aviary problem

    for md_file in sorted(Path(f"{reports_dir}subsystems").glob("*.md"), key=str):
        subsystems_pane = create_report_frame("markdown", str(
            md_file),
            f'''
        The subsystems report tab shows selected results for every major subsystem in the Aviary problem.
        This report is for the {md_file.stem} subsystem. Reports available currently are mass, geometry, and propulsion.
            ''')
        subsystem_tabs_list.append((md_file.stem, subsystems_pane))

    # Actually make the tabs from the list of Panes
    model_tabs = pn.Tabs(*model_tabs_list, stylesheets=["assets/aviary_styles.css"])
    optimization_tabs = pn.Tabs(
        *optimization_tabs_list, stylesheets=["assets/aviary_styles.css"]
    )
    results_tabs = pn.Tabs(*results_tabs_list, stylesheets=["assets/aviary_styles.css"])
    results_tabs.active = 2  # make the Results tab active initially
    if subsystem_tabs_list:
        subsystem_tabs = pn.Tabs(
            *subsystem_tabs_list, stylesheets=["assets/aviary_styles.css"]
        )

    # Add subtabs to tabs
    high_level_tabs = []
    high_level_tabs.append(("Results", results_tabs))
    if subsystem_tabs_list:
        high_level_tabs.append(("Subsystems", subsystem_tabs))
    high_level_tabs.append(("Model", model_tabs))
    high_level_tabs.append(("Optimization", optimization_tabs))
    tabs = pn.Tabs(*high_level_tabs, stylesheets=["assets/aviary_styles.css"])
    tabs.active = 0  # make the Results tab active initially

    template = pn.template.FastListTemplate(
        title=f"Aviary Dashboard for {script_name}",
        logo="assets/aviary_logo.png",
        favicon="assets/aviary_logo.png",
        main=[tabs],
        accent_base_color="black",
        header_background="rgb(0, 212, 169)",
        background_color="white",
        theme=DefaultTheme,
        theme_toggle=False,
        main_layout=None,
        css_files=["assets/aviary_styles.css"],
    )

    if env_truthy("TESTFLO_RUNNING"):
        show = False
        threaded = True
    else:
        show = True
        threaded = False

    assets_dir = pathlib.Path(
        importlib.util.find_spec("aviary").origin
    ).parent.joinpath("visualization/assets/")
    home_dir = "."
    if port == 0:
        port = get_free_port()
    server = pn.serve(
        template,
        port=port,
        address="localhost",
        websocket_origin=f"localhost:{port}",
        show=show,
        threaded=threaded,
        static_dirs={
            "reports": reports_dir,
            "home": home_dir,
            "assets": assets_dir,
        },
    )
    server.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    _dashboard_setup_parser(parser)
    args = parser.parse_args()
    _dashboard_cmd(args, None)
