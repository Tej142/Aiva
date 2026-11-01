package com.example.checkbox;

import android.os.Bundle;

import androidx.activity.EdgeToEdge;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;

import android.view.View;
import android.widget.*;

import org.w3c.dom.Text;

public class MainActivity extends AppCompatActivity {

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        CheckBox ch1 = findViewById(R.id.ch1);
        CheckBox ch2 = findViewById(R.id.ch2);
        TextView tv = findViewById(R.id.tv);
        Button bt = findViewById(R.id.bt);

        bt.setOnClickListener(new View.OnClickListener(){
            public void onClick(View v){
                String list = tv.getText().toString();
                if(ch1.isChecked()){
                    list = list + ch1.getText().toString();
                }
                if(ch2.isChecked()){
                    list = list + ch2.getText().toString();
                }
                tv.setText(list);
            }
        });
    }
}