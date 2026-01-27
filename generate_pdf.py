#!/usr/bin/env python3
"""
Generate PDF documentation from CHANGELOG.md and TESTING.md
"""
from reportlab.lib.pagesizes import letter, A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.lib.colors import HexColor
import re

def markdown_to_paragraphs(text, styles):
    """Convert markdown text to ReportLab Paragraph objects"""
    paragraphs = []
    lines = text.split('\n')
    
    for line in lines:
        line = line.strip()
        if not line:
            paragraphs.append(Spacer(1, 0.1*inch))
            continue
            
        # Headers
        if line.startswith('# '):
            paragraphs.append(Paragraph(line[2:], styles['Heading1']))
            paragraphs.append(Spacer(1, 0.2*inch))
        elif line.startswith('## '):
            paragraphs.append(Paragraph(line[3:], styles['Heading2']))
            paragraphs.append(Spacer(1, 0.15*inch))
        elif line.startswith('### '):
            paragraphs.append(Paragraph(line[4:], styles['Heading3']))
            paragraphs.append(Spacer(1, 0.1*inch))
        elif line.startswith('#### '):
            paragraphs.append(Paragraph(line[5:], styles['Heading4']))
            paragraphs.append(Spacer(1, 0.08*inch))
        # Bullet points
        elif line.startswith('- '):
            # Convert markdown bold **text** to <b>text</b>
            line = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', line)
            paragraphs.append(Paragraph('• ' + line[2:], styles['Normal']))
            paragraphs.append(Spacer(1, 0.05*inch))
        # Checkboxes
        elif line.startswith('- [ ]'):
            line = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', line)
            paragraphs.append(Paragraph('☐ ' + line[5:], styles['Normal']))
            paragraphs.append(Spacer(1, 0.05*inch))
        elif line.startswith('- [x]') or line.startswith('- [X]'):
            line = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', line)
            paragraphs.append(Paragraph('☑ ' + line[5:], styles['Normal']))
            paragraphs.append(Spacer(1, 0.05*inch))
        # Code blocks (skip for now, just show as normal)
        elif line.startswith('```'):
            continue
        # Horizontal rule
        elif line.startswith('---'):
            paragraphs.append(Spacer(1, 0.2*inch))
        else:
            # Regular text - convert markdown bold
            line = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', line)
            # Convert code backticks
            line = re.sub(r'`(.*?)`', r'<font name="Courier">\1</font>', line)
            paragraphs.append(Paragraph(line, styles['Normal']))
            paragraphs.append(Spacer(1, 0.05*inch))
    
    return paragraphs

def create_pdf():
    """Create PDF document from markdown files"""
    
    # Read markdown files
    with open('CHANGELOG.md', 'r', encoding='utf-8') as f:
        changelog_text = f.read()
    
    with open('TESTING.md', 'r', encoding='utf-8') as f:
        testing_text = f.read()
    
    # Create PDF
    pdf_file = 'ArcTrigger_MV1.0_Documentation.pdf'
    doc = SimpleDocTemplate(pdf_file, pagesize=letter,
                          rightMargin=72, leftMargin=72,
                          topMargin=72, bottomMargin=18)
    
    # Container for the 'Flowable' objects
    story = []
    
    # Define styles
    styles = getSampleStyleSheet()
    
    # Custom styles
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=24,
        textColor=HexColor('#A020F0'),
        spaceAfter=30,
        alignment=TA_CENTER
    )
    
    heading1_style = ParagraphStyle(
        'CustomHeading1',
        parent=styles['Heading1'],
        fontSize=18,
        textColor=HexColor('#A020F0'),
        spaceAfter=12
    )
    
    heading2_style = ParagraphStyle(
        'CustomHeading2',
        parent=styles['Heading2'],
        fontSize=14,
        textColor=HexColor('#000080'),
        spaceAfter=10
    )
    
    heading3_style = ParagraphStyle(
        'CustomHeading3',
        parent=styles['Heading3'],
        fontSize=12,
        textColor=HexColor('#006400'),
        spaceAfter=8
    )
    
    # Add title page
    story.append(Spacer(1, 2*inch))
    story.append(Paragraph("ArcTrigger", title_style))
    story.append(Spacer(1, 0.3*inch))
    story.append(Paragraph("MoneyVersion 1.0", styles['Heading2']))
    story.append(Spacer(1, 0.5*inch))
    story.append(Paragraph("Documentation", styles['Normal']))
    story.append(PageBreak())
    
    # Add changelog
    story.append(Paragraph("CHANGELOG", heading1_style))
    story.append(Spacer(1, 0.2*inch))
    changelog_paras = markdown_to_paragraphs(changelog_text, {
        'Heading1': heading1_style,
        'Heading2': heading2_style,
        'Heading3': heading3_style,
        'Heading4': styles['Heading4'],
        'Normal': styles['Normal']
    })
    story.extend(changelog_paras)
    
    story.append(PageBreak())
    
    # Add testing checklist
    story.append(Paragraph("TESTING CHECKLIST", heading1_style))
    story.append(Spacer(1, 0.2*inch))
    testing_paras = markdown_to_paragraphs(testing_text, {
        'Heading1': heading1_style,
        'Heading2': heading2_style,
        'Heading3': heading3_style,
        'Heading4': styles['Heading4'],
        'Normal': styles['Normal']
    })
    story.extend(testing_paras)
    
    # Build PDF
    doc.build(story)
    print(f"PDF created: {pdf_file}")

if __name__ == '__main__':
    try:
        create_pdf()
    except ImportError:
        print("Installing reportlab...")
        import subprocess
        subprocess.check_call(['pip', 'install', 'reportlab'])
        create_pdf()
